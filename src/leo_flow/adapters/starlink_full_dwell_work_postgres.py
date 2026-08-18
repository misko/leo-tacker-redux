"""PostgreSQL durable work adapter for optional full-dwell production."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.rows import dict_row

from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    RecordingId,
    UtcNs,
)
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteProductRefV0_2,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.services.starlink_full_dwell_producer import (
    FullDwellAdmissionResultV0_1,
    FullDwellWorkLeaseV0_1,
    StaleFullDwellWorkLeaseError,
)

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresFullDwellWorkRepositoryV0_1:
    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._connect = connect
        self._token = token_factory or (lambda: f"slfdlease_{uuid.uuid4().hex}")

    def admit(
        self, *, maximum_new: int, maximum_active: int
    ) -> FullDwellAdmissionResultV0_1:
        with self._connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
            rows = cursor.execute(
                "SELECT * FROM public.admit_starlink_full_dwell_work_v0_1(%s,%s)",
                (maximum_new, maximum_active),
            ).fetchall()
        if len(rows) != 1:
            raise RuntimeError("full-dwell admission outcome is ambiguous")
        row = rows[0]
        return FullDwellAdmissionResultV0_1(
            _integer(row["admitted"]),
            _integer(row["active_backlog"]),
            _boolean(row["saturated"]),
        )

    def claim(
        self, worker_id: str, lease_ttl_s: float
    ) -> FullDwellWorkLeaseV0_1 | None:
        if not worker_id or lease_ttl_s <= 0:
            raise ValueError("full-dwell claim bounds are invalid")
        token = f"{worker_id}:{self._token()}"
        with self._connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
            rows = cursor.execute(
                "SELECT * FROM public.claim_starlink_full_dwell_work_v0_1(%s,%s)",
                (token, timedelta(seconds=lease_ttl_s)),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("full-dwell claim is ambiguous")
        row = rows[0]
        bundle = ObjectRef(
            Digest(
                DigestAlgorithm(str(row["bundle_digest_algorithm"])),
                str(row["bundle_digest_value"]),
            ),
            _integer(row["bundle_byte_count"]),
            str(row["bundle_media_type"]),
            str(row["bundle_format_id"]),
            str(row["bundle_locator"]),
        )
        return FullDwellWorkLeaseV0_1(
            StarlinkDetectorSuiteProductRefV0_2(
                str(row["source_suite_analysis_id"]),
                RecordingId(str(row["recording_id"])),
                bundle,
            ),
            Digest(
                DigestAlgorithm.SHA256,
                str(row["source_suite_request_digest_value"]),
            ),
            str(row["lease_token"]),
            _integer(row["lease_generation"]),
            _integer(row["attempt"]),
        )

    def complete(self, lease: FullDwellWorkLeaseV0_1, result: ArtifactRef) -> None:
        self._transition(
            "complete_starlink_full_dwell_work_v0_1",
            lease,
            result.artifact_id,
        )

    def retry(
        self,
        lease: FullDwellWorkLeaseV0_1,
        reason: str,
        retry_at_utc_ns: UtcNs,
    ) -> None:
        self._transition(
            "retry_starlink_full_dwell_work_v0_1",
            lease,
            reason,
            _ns_to_datetime(retry_at_utc_ns),
        )

    def park(self, lease: FullDwellWorkLeaseV0_1, reason: str) -> None:
        self._transition("park_starlink_full_dwell_work_v0_1", lease, reason)

    def _transition(
        self, function: str, lease: FullDwellWorkLeaseV0_1, *extra: object
    ) -> None:
        placeholders = ",%s" * len(extra)
        with self._connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
            row = cursor.execute(
                f"SELECT public.{function}(%s,%s,%s{placeholders}) AS changed",
                (
                    lease.source_suite_ref.analysis_id,
                    lease.lease_token,
                    lease.lease_generation,
                    *extra,
                ),
            ).fetchone()
        if row is None or row["changed"] is not True:
            raise StaleFullDwellWorkLeaseError("full-dwell work lease is stale")


def _integer(value: object) -> int:
    if isinstance(value, bool):
        raise TypeError("database integer cannot be boolean")
    return int(str(value))


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("database boolean is invalid")
    return value


def _ns_to_datetime(value: UtcNs) -> datetime:
    seconds, nanoseconds = divmod(int(value), 1_000_000_000)
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=nanoseconds // 1000)
