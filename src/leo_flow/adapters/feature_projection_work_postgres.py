"""PostgreSQL adapter for durable FeatureSet projection work."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.rows import dict_row

from leo_flow.application.feature_projection_work import (
    FEATURE_PROJECTION_WORK_SCHEMA,
    FeatureProjectionWork,
    FeatureProjectionWorkLease,
    StaleFeatureProjectionLeaseError,
)
from leo_flow.contracts.core import (
    AnalysisRunId,
    Digest,
    DigestAlgorithm,
    FeatureSetId,
    JobId,
    RecordingId,
    UtcNs,
)
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.jobs.contracts import JobLease

from . import feature_projection_work_postgres_sql as sql

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresFeatureProjectionWorkError(RuntimeError):
    """Database returned malformed or contradictory projection work."""


class PostgresFeatureProjectionWorkRepository:
    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        token_factory: Callable[[], str] | None = None,
        source_job_ids: tuple[JobId, ...] | None = None,
    ) -> None:
        self._connect = connect
        self._token = token_factory or (lambda: f"fplease_{secrets.token_hex(16)}")
        if source_job_ids is not None and (
            not 1 <= len(source_job_ids) <= 72
            or len(set(source_job_ids)) != len(source_job_ids)
        ):
            raise ValueError("feature projection scope requires 1..72 unique jobs")
        self._source_job_ids = source_job_ids

    def claim(self, worker_id: str, ttl_s: float) -> FeatureProjectionWorkLease | None:
        del worker_id
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                sql.CLAIM_SQL if self._source_job_ids is None else sql.CLAIM_SCOPED_SQL,
                {
                    "lease_token": self._token(),
                    "ttl_interval": _ttl(ttl_s),
                    "source_job_ids": (
                        None
                        if self._source_job_ids is None
                        else [str(value) for value in self._source_job_ids]
                    ),
                },
            )
            row = cursor.fetchone()
        return None if row is None else _lease(row)

    def heartbeat(
        self,
        work_id: str,
        lease_token: str,
        generation: int,
        ttl_s: float,
    ) -> FeatureProjectionWorkLease:
        row = self._one_transition(
            sql.HEARTBEAT_SQL,
            {
                "work_id": work_id,
                "lease_token": lease_token,
                "lease_generation": generation,
                "ttl_interval": _ttl(ttl_s),
            },
        )
        return _lease(row)

    def complete(self, work_id: str, lease_token: str, generation: int) -> None:
        self._boolean_transition(
            sql.COMPLETE_SQL,
            _lease_parameters(work_id, lease_token, generation),
        )

    def retry(
        self,
        work_id: str,
        lease_token: str,
        generation: int,
        reason: str,
        delay_s: float,
    ) -> None:
        self._boolean_transition(
            sql.RETRY_SQL,
            {
                **_lease_parameters(work_id, lease_token, generation),
                "reason": reason,
                "delay_interval": _ttl(delay_s),
            },
        )

    def park(
        self, work_id: str, lease_token: str, generation: int, reason: str
    ) -> None:
        self._boolean_transition(
            sql.PARK_SQL,
            {
                **_lease_parameters(work_id, lease_token, generation),
                "reason": reason,
            },
        )

    def _one_transition(
        self, statement: str, parameters: dict[str, object]
    ) -> dict[str, object]:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(statement, parameters)
            row = cursor.fetchone()
        if row is None:
            raise StaleFeatureProjectionLeaseError(
                "projection lease token, generation, or expiry is stale"
            )
        return row

    def _boolean_transition(
        self, statement: str, parameters: dict[str, object]
    ) -> None:
        row = self._one_transition(statement, parameters)
        outcome = next(iter(row.values()))
        if outcome is not True:
            raise StaleFeatureProjectionLeaseError(
                "projection lease token, generation, or expiry is stale"
            )


def enqueue_feature_projection_with_cursor(
    cursor: psycopg.Cursor[dict[str, object]],
    source_lease: JobLease,
    feature_ref: FeatureSetRef,
    recording_ref: RecordingObjectRef,
) -> str:
    """Create exact work inside the caller's FeatureSet/job transaction."""

    work_id = _work_id(source_lease)
    cursor.execute(
        sql.PUBLISH_SQL,
        {
            "work_id": work_id,
            "source_job_id": str(source_lease.job_id),
            "source_lease_token": source_lease.lease_token,
            "source_lease_generation": source_lease.lease_generation,
            "feature_set_id": str(feature_ref.feature_set_id),
            "analysis_run_id": str(feature_ref.analysis_run_id),
            "feature_digest_algorithm": feature_ref.bundle_ref.digest.algorithm.value,
            "feature_digest_value": feature_ref.bundle_ref.digest.value,
            "recording_id": str(recording_ref.recording_id),
            "recording_digest_algorithm": (
                recording_ref.identity_digest().algorithm.value
            ),
            "recording_digest_value": recording_ref.identity_digest().value,
        },
    )
    row = cursor.fetchone()
    if row is None or next(iter(row.values())) is not True:
        raise PostgresFeatureProjectionWorkError(
            "projection work publication returned no exact outcome"
        )
    return work_id


def connection_factory(dsn: str) -> ConnectionFactory:
    return lambda: psycopg.connect(dsn, row_factory=dict_row)


def _work_id(lease: JobLease) -> str:
    digest = Digest.sha256(str(lease.job_id).encode("utf-8"))
    return f"fpwork_{digest.value}"


def _lease(row: dict[str, object]) -> FeatureProjectionWorkLease:
    expires = row.get("lease_expires_utc")
    if not isinstance(expires, datetime):
        raise PostgresFeatureProjectionWorkError(
            "leased projection work has no database expiry"
        )
    schema_id = str(row["work_schema_id"])
    schema_version = str(row["work_schema_version"])
    if schema_id != FEATURE_PROJECTION_WORK_SCHEMA.schema_id or schema_version != str(
        FEATURE_PROJECTION_WORK_SCHEMA.version
    ):
        raise PostgresFeatureProjectionWorkError(
            "database projection work schema is unsupported"
        )
    bundle_ref = ObjectRef(
        _digest(row, "feature"),
        _database_int(row["feature_byte_count"], "feature_byte_count"),
        str(row["feature_media_type"]),
        str(row["feature_format_id"]),
        str(row["feature_locator"]),
    )
    item = FeatureProjectionWork(
        FEATURE_PROJECTION_WORK_SCHEMA,
        str(row["work_id"]),
        JobId(str(row["source_job_id"])),
        FeatureSetRef(
            FeatureSetId(str(row["feature_set_id"])),
            AnalysisRunId(str(row["analysis_run_id"])),
            bundle_ref,
        ),
        RecordingId(str(row["recording_id"])),
        _digest(row, "recording"),
    )
    return FeatureProjectionWorkLease(
        item,
        _database_int(row["attempt"], "attempt"),
        str(row["lease_token"]),
        _database_int(row["lease_generation"], "lease_generation"),
        _datetime_to_ns(expires),
    )


def _digest(row: dict[str, object], prefix: str) -> Digest:
    return Digest(
        DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
        str(row[f"{prefix}_digest_value"]),
    )


def _lease_parameters(
    work_id: str, lease_token: str, generation: int
) -> dict[str, object]:
    return {
        "work_id": work_id,
        "lease_token": lease_token,
        "lease_generation": generation,
    }


def _ttl(ttl_s: float) -> timedelta:
    if ttl_s <= 0:
        raise ValueError("lease or retry interval must be positive")
    result = timedelta(seconds=ttl_s)
    if result <= timedelta(0):
        raise ValueError("interval is below PostgreSQL timestamp resolution")
    return result


def _database_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PostgresFeatureProjectionWorkError(f"database {name} is not an integer")
    return value


def _datetime_to_ns(value: datetime) -> UtcNs:
    if value.tzinfo is None:
        raise PostgresFeatureProjectionWorkError(
            "database projection lease expiry has no timezone"
        )
    utc = value.astimezone(UTC)
    return UtcNs(int(utc.timestamp()) * 1_000_000_000 + utc.microsecond * 1_000)
