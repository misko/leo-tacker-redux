"""Fenced PostgreSQL work queue for durable waterfall dashboard projection."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import timedelta

import psycopg

from leo_flow.application.waterfall_projection_work import (
    WaterfallProjectionLeaseV0_1,
)
from leo_flow.contracts.core import JobId

from .waterfall_receipt_postgres import PostgresWaterfallReceiptReaderV0_1

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresWaterfallProjectionWorkRepositoryV0_1:
    """Use only the fenced 0025 functions; never expose its private table."""

    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        source_job_ids: tuple[JobId, ...] | None = None,
    ) -> None:
        self._connect = connect
        self._receipts = PostgresWaterfallReceiptReaderV0_1(connect)
        if source_job_ids is not None and (
            not 1 <= len(source_job_ids) <= 72
            or len(set(source_job_ids)) != len(source_job_ids)
        ):
            raise ValueError("waterfall projection scope requires 1..72 unique jobs")
        self._source_job_ids = source_job_ids

    def claim(
        self, worker_id: str, lease_ttl_s: float
    ) -> WaterfallProjectionLeaseV0_1 | None:
        if not worker_id or lease_ttl_s <= 0:
            raise ValueError("waterfall projection claim bounds must be positive")
        token = f"{worker_id}:{uuid.uuid4().hex}"
        with self._connect() as connection:
            if self._source_job_ids is None:
                rows = connection.execute(
                    "SELECT * FROM public.claim_waterfall_projection_work(%s, %s)",
                    (token, timedelta(seconds=lease_ttl_s)),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM public.claim_campaign_waterfall_projection(%s,%s,%s)",
                    (
                        [str(value) for value in self._source_job_ids],
                        token,
                        timedelta(seconds=lease_ttl_s),
                    ),
                ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("waterfall projection claim is ambiguous")
        row = rows[0]
        job_id = JobId(_text(row, "source_job_id"))
        receipt = self._receipts.read(job_id)
        if (
            receipt is None
            or receipt.work_id != _text(row, "work_id")
            or str(receipt.waterfall_ref.product_id) != _text(row, "product_id")
            or str(receipt.waterfall_ref.analysis_run_id)
            != _text(row, "analysis_run_id")
            or str(receipt.waterfall_ref.recording_id) != _text(row, "recording_id")
        ):
            raise RuntimeError("claimed waterfall work contradicts its durable receipt")
        return WaterfallProjectionLeaseV0_1(
            receipt.work_id,
            job_id,
            receipt.waterfall_ref,
            token,
            _integer(row, "lease_generation"),
            _integer(row, "attempt"),
        )

    def complete(self, lease: WaterfallProjectionLeaseV0_1) -> None:
        self._transition(
            "SELECT public.complete_waterfall_projection_work(%s,%s,%s) AS changed",
            (lease.work_id, lease.lease_token, lease.lease_generation),
        )

    def retry(
        self, lease: WaterfallProjectionLeaseV0_1, reason: str, delay_s: float
    ) -> None:
        if delay_s <= 0:
            raise ValueError("waterfall projection retry delay must be positive")
        self._transition(
            "SELECT public.retry_waterfall_projection_work(%s,%s,%s,%s,%s) AS changed",
            (
                lease.work_id,
                lease.lease_token,
                lease.lease_generation,
                reason,
                timedelta(seconds=delay_s),
            ),
        )

    def park(self, lease: WaterfallProjectionLeaseV0_1, reason: str) -> None:
        self._transition(
            "SELECT public.park_waterfall_projection_work(%s,%s,%s,%s) AS changed",
            (lease.work_id, lease.lease_token, lease.lease_generation, reason),
        )

    def _transition(self, statement: str, parameters: tuple[object, ...]) -> None:
        with self._connect() as connection:
            row = connection.execute(statement, parameters).fetchone()
        if row is None or row["changed"] is not True:
            raise RuntimeError("waterfall projection lease is stale")


def _text(row: dict[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"waterfall projection {field} is invalid")
    return value


def _integer(row: dict[str, object], field: str) -> int:
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"waterfall projection {field} is invalid")
    return value
