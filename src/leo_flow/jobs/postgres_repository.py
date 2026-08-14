"""Psycopg JobLeaseRepository with database-enforced generation fencing."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    JobId,
    SchemaRef,
    SchemaVersion,
    UtcNs,
)

from . import postgres_sql
from .contracts import (
    JobLease,
    JobPayload,
    JobSnapshot,
    JobState,
    JobType,
    validate_park_reason,
)
from .ports import StaleLeaseError


class PostgresJobError(RuntimeError):
    pass


class JobConflictError(PostgresJobError):
    pass


ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresJobLeaseRepository:
    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._connect = connect
        self._token = token_factory or (lambda: f"lease_{secrets.token_hex(16)}")

    def enqueue(
        self,
        job_id: JobId,
        job_type: JobType,
        payload: JobPayload,
        *,
        available_at_utc_ns: UtcNs | None = None,
    ) -> None:
        available = (
            datetime.now(UTC)
            if available_at_utc_ns is None
            else _ns_to_datetime(available_at_utc_ns)
        )
        payload_value = _json_value(payload.value)
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                postgres_sql.ENQUEUE_SQL,
                {
                    "job_id": str(job_id),
                    "job_type": job_type.value,
                    "payload_schema_id": payload.schema.schema_id,
                    "payload_schema_version": str(payload.schema.version),
                    "payload": Jsonb(payload_value),
                    "available_at_utc": available,
                },
            )
            inserted = cursor.fetchone()
            if inserted is None:
                raise PostgresJobError("enqueue function returned no outcome")
            if next(iter(inserted.values())) is True:
                return
            cursor.execute("SELECT * FROM job WHERE job_id = %s", (str(job_id),))
            row = cursor.fetchone()
            if row is None or not _same_job(row, job_type, payload):
                raise JobConflictError("job ID identifies a different payload")

    def claim(
        self, types: tuple[JobType, ...], worker_id: str, ttl_s: float
    ) -> JobLease | None:
        del worker_id
        if not types:
            return None
        ttl = _ttl(ttl_s)
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                postgres_sql.CLAIM_SQL,
                {
                    "job_types": [job_type.value for job_type in types],
                    "lease_token": self._token(),
                    "ttl_interval": ttl,
                },
            )
            row = cursor.fetchone()
            return None if row is None else _lease_from_row(row)

    def heartbeat(
        self, job_id: JobId, lease_token: str, generation: int, ttl_s: float
    ) -> JobLease:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                postgres_sql.HEARTBEAT_SQL,
                {
                    "job_id": str(job_id),
                    "lease_token": lease_token,
                    "lease_generation": generation,
                    "ttl_interval": _ttl(ttl_s),
                },
            )
            row = cursor.fetchone()
            if row is None:
                raise StaleLeaseError("lease token, generation, or expiry is stale")
            return _lease_from_row(row)

    def complete(
        self,
        job_id: JobId,
        lease_token: str,
        generation: int,
        result_ref: ArtifactRef,
    ) -> None:
        self._fenced_update(
            postgres_sql.COMPLETE_SQL,
            {
                "job_id": str(job_id),
                "lease_token": lease_token,
                "lease_generation": generation,
                "result_ref": Jsonb(_artifact_value(result_ref)),
            },
        )

    def fail(
        self,
        job_id: JobId,
        lease_token: str,
        generation: int,
        reason: str,
        retry_at_utc_ns: UtcNs | None,
    ) -> None:
        if not reason:
            raise ValueError("failure reason cannot be empty")
        retry_at = (
            datetime.now(UTC)
            if retry_at_utc_ns is None
            else _ns_to_datetime(retry_at_utc_ns)
        )
        self._fenced_update(
            postgres_sql.FAIL_SQL,
            {
                "job_id": str(job_id),
                "lease_token": lease_token,
                "lease_generation": generation,
                "reason": reason,
                "retry_at_utc": retry_at,
            },
        )

    def park(
        self,
        job_id: JobId,
        lease_token: str,
        generation: int,
        reason: str,
    ) -> None:
        validate_park_reason(reason)
        self._fenced_update(
            postgres_sql.PARK_SQL,
            {
                "job_id": str(job_id),
                "lease_token": lease_token,
                "lease_generation": generation,
                "reason": reason,
            },
        )

    def snapshot(self, job_id: JobId) -> JobSnapshot:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(postgres_sql.SNAPSHOT_SQL, {"job_id": str(job_id)})
            row = cursor.fetchone()
        if row is None:
            raise KeyError(job_id)
        return _snapshot_from_row(row)

    def _fenced_update(self, sql: str, parameters: dict[str, object]) -> None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(sql, parameters)
            if cursor.fetchone() is None:
                raise StaleLeaseError("lease token, generation, or expiry is stale")


def connection_factory(dsn: str) -> ConnectionFactory:
    return lambda: psycopg.connect(dsn, row_factory=dict_row)


def _ttl(ttl_s: float) -> timedelta:
    if ttl_s <= 0:
        raise ValueError("lease TTL must be positive")
    result = timedelta(seconds=ttl_s)
    if result <= timedelta(0):
        raise ValueError("lease TTL is below PostgreSQL timestamp resolution")
    return result


def _lease_from_row(row: dict[str, object]) -> JobLease:
    expires = row["lease_expires_utc"]
    if not isinstance(expires, datetime):
        raise PostgresJobError("leased row has no database expiry")
    return JobLease(
        JobId(str(row["job_id"])),
        JobType(str(row["job_type"])),
        JobPayload(
            SchemaRef(
                str(row["payload_schema_id"]),
                SchemaVersion.parse(str(row["payload_schema_version"])),
            ),
            _freeze_object(row["payload"]),
        ),
        _database_int(row["attempt"], "attempt"),
        str(row["lease_token"]),
        _database_int(row["lease_generation"], "lease_generation"),
        _datetime_to_ns(expires),
    )


def _same_job(row: dict[str, object], job_type: JobType, payload: JobPayload) -> bool:
    return (
        row["job_type"] == job_type.value
        and row["payload_schema_id"] == payload.schema.schema_id
        and row["payload_schema_version"] == str(payload.schema.version)
        and row["payload"] == _json_value(payload.value)
    )


def _snapshot_from_row(row: dict[str, object]) -> JobSnapshot:
    parked_at = row["parked_at_utc"]
    if parked_at is not None and not isinstance(parked_at, datetime):
        raise PostgresJobError("database parked_at_utc is not a timestamp")
    result_value = row["result_ref"]
    return JobSnapshot(
        JobId(str(row["job_id"])),
        JobState(str(row["state"])),
        _database_int(row["attempt"], "attempt"),
        _database_int(row["lease_generation"], "lease_generation"),
        None if result_value is None else _artifact_from_value(result_value),
        _optional_string(row["last_error"], "last_error"),
        _optional_string(row["park_reason"], "park_reason"),
        None if parked_at is None else _datetime_to_ns(parked_at),
    )


def _artifact_from_value(value: object) -> ArtifactRef:
    if not isinstance(value, dict):
        raise PostgresJobError("database result_ref is not an object")
    try:
        artifact_id = value["artifact_id"]
        algorithm = value["digest_algorithm"]
        digest_value = value["digest_value"]
    except KeyError as error:
        raise PostgresJobError("database result_ref is incomplete") from error
    schema_id = value.get("schema_id")
    schema_version = value.get("schema_version")
    if not all(
        isinstance(item, str) for item in (artifact_id, algorithm, digest_value)
    ):
        raise PostgresJobError("database result_ref identity is invalid")
    if (schema_id is None) != (schema_version is None):
        raise PostgresJobError("database result_ref schema is incomplete")
    schema = None
    if schema_id is not None and schema_version is not None:
        if not isinstance(schema_id, str) or not isinstance(schema_version, str):
            raise PostgresJobError("database result_ref schema is invalid")
        schema = SchemaRef(schema_id, SchemaVersion.parse(schema_version))
    return ArtifactRef(
        artifact_id,
        Digest(DigestAlgorithm(algorithm), digest_value),
        schema,
    )


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PostgresJobError(f"database {field} is not a string")
    return value


def _artifact_value(ref: ArtifactRef) -> dict[str, object]:
    value: dict[str, object] = {
        "artifact_id": ref.artifact_id,
        "digest_algorithm": ref.digest.algorithm.value,
        "digest_value": ref.digest.value,
    }
    if ref.schema is not None:
        value["schema_id"] = ref.schema.schema_id
        value["schema_version"] = str(ref.schema.version)
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, tuple):
        if all(
            isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str)
            for item in value
        ):
            return {key: _json_value(item) for key, item in value}
        return [_json_value(item) for item in value]
    return value


def _freeze_object(value: object) -> tuple[tuple[str, Any], ...]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PostgresJobError("job payload JSON must be an object")
    return tuple((key, _freeze_value(item)) for key, item in sorted(value.items()))


def _freeze_value(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple((key, _freeze_value(item)) for key, item in sorted(value.items()))
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


def _ns_to_datetime(value: UtcNs) -> datetime:
    seconds, nanoseconds = divmod(int(value), 1_000_000_000)
    return datetime.fromtimestamp(seconds, UTC).replace(
        microsecond=nanoseconds // 1_000
    )


def _datetime_to_ns(value: datetime) -> UtcNs:
    if value.tzinfo is None:
        raise PostgresJobError("database timestamp is not timezone-aware")
    utc = value.astimezone(UTC)
    seconds = int(utc.timestamp())
    return UtcNs(seconds * 1_000_000_000 + utc.microsecond * 1_000)


def _database_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PostgresJobError(f"database {field} is not an integer")
    return value
