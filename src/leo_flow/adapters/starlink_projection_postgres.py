"""Fenced PostgreSQL outbox for Starlink dashboard projection."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import timedelta

import psycopg

from leo_flow.application.starlink_projection_work import StarlinkProjectionLeaseV0_1
from leo_flow.contracts.core import Digest, DigestAlgorithm, JobId, RecordingId
from leo_flow.contracts.starlink_pipeline import StarlinkPilotAnalysisProductRefV0_1
from leo_flow.contracts.storage import ObjectRef

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresStarlinkProjectionWorkRepositoryV0_1:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def claim(
        self, worker_id: str, lease_ttl_s: float
    ) -> StarlinkProjectionLeaseV0_1 | None:
        if not worker_id or lease_ttl_s <= 0:
            raise ValueError("Starlink projection claim bounds must be positive")
        token = f"{worker_id}:{uuid.uuid4().hex}"
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM public.claim_starlink_projection_work(%s,%s)",
                (token, timedelta(seconds=lease_ttl_s)),
            ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise RuntimeError("Starlink projection claim is ambiguous")
            row = rows[0]
            receipts = connection.execute(
                "SELECT * FROM public.read_starlink_analysis_receipt(%s)",
                (row["source_job_id"],),
            ).fetchall()
        if len(receipts) != 1:
            raise RuntimeError("Starlink projection receipt is missing or ambiguous")
        receipt = receipts[0]
        if (
            receipt["work_id"] != row["work_id"]
            or receipt["analysis_id"] != row["analysis_id"]
        ):
            raise RuntimeError("Starlink work contradicts its receipt")
        ref = StarlinkPilotAnalysisProductRefV0_1(
            str(receipt["analysis_id"]),
            RecordingId(str(receipt["recording_id"])),
            ObjectRef(
                Digest(
                    DigestAlgorithm(str(receipt["bundle_digest_algorithm"])),
                    str(receipt["bundle_digest_value"]),
                ),
                _integer(receipt["bundle_byte_count"]),
                str(receipt["bundle_media_type"]),
                str(receipt["bundle_format_id"]),
                str(receipt["bundle_locator"]),
            ),
        )
        return StarlinkProjectionLeaseV0_1(
            str(row["work_id"]),
            JobId(str(row["source_job_id"])),
            ref,
            token,
            _integer(row["lease_generation"]),
            _integer(row["attempt"]),
        )

    def complete(self, lease: StarlinkProjectionLeaseV0_1) -> None:
        self._transition(
            "SELECT public.complete_starlink_projection_work(%s,%s,%s) AS changed",
            (lease.work_id, lease.lease_token, lease.lease_generation),
        )

    def retry(
        self, lease: StarlinkProjectionLeaseV0_1, reason: str, delay_s: float
    ) -> None:
        if delay_s <= 0:
            raise ValueError("Starlink retry delay must be positive")
        self._transition(
            "SELECT public.retry_starlink_projection_work(%s,%s,%s,%s,%s) AS changed",
            (
                lease.work_id,
                lease.lease_token,
                lease.lease_generation,
                reason,
                timedelta(seconds=delay_s),
            ),
        )

    def park(self, lease: StarlinkProjectionLeaseV0_1, reason: str) -> None:
        self._transition(
            "SELECT public.park_starlink_projection_work(%s,%s,%s,%s) AS changed",
            (lease.work_id, lease.lease_token, lease.lease_generation, reason),
        )

    def _transition(self, statement: str, parameters: tuple[object, ...]) -> None:
        with self._connect() as connection:
            row = connection.execute(statement, parameters).fetchone()
        if row is None or row["changed"] is not True:
            raise RuntimeError("Starlink projection lease is stale")


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Starlink projection count is invalid")
    return value
