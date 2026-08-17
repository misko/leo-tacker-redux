"""Narrow PostgreSQL claims scoped to one exact deferred-analysis window."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.rows import dict_row

from leo_flow.contracts.core import JobId, SchemaRef, SchemaVersion, UtcNs
from leo_flow.contracts.deferred_analysis import (
    DeferredAnalysisStage,
    DeferredAnalysisWindowV1,
)
from leo_flow.jobs.contracts import JobLease, JobPayload, JobType

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresCampaignScopedJobClaimsV1:
    """Claim only one of the caller's finite exact job identities."""

    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._connect = connect
        self._token = token_factory or (
            lambda: f"campaignlease_{secrets.token_hex(16)}"
        )

    def claim(
        self,
        job_ids: Sequence[JobId],
        expected_type: JobType,
        worker_id: str,
        ttl_s: float,
    ) -> JobLease | None:
        del worker_id
        ids = _exact_ids(job_ids)
        if ttl_s <= 0:
            raise ValueError("campaign claim TTL must be positive")
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT * FROM public.claim_campaign_analysis_job(%s,%s,%s,%s)",
                (ids, expected_type.value, self._token(), timedelta(seconds=ttl_s)),
            )
            rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("campaign-scoped job claim is ambiguous")
        lease = _job_lease(rows[0])
        if lease.job_id not in job_ids or lease.job_type is not expected_type:
            raise RuntimeError("campaign-scoped job claim escaped its exact scope")
        return lease


class PostgresCampaignAnalysisLaneStateReaderV1:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def states(
        self, window: DeferredAnalysisWindowV1, stage: DeferredAnalysisStage
    ) -> dict[str, str]:
        job_ids = _stage_job_ids(window, stage)
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT * FROM public.read_campaign_analysis_lane_status(%s,%s)",
                (stage.value, [str(value) for value in job_ids]),
            )
            rows = cursor.fetchall()
        states: dict[str, str] = {}
        allowed = {"ready", "leased", "failed", "succeeded", "parked"}
        for row in rows:
            identity, state = str(row["identity_id"]), str(row["state"])
            if identity in states or identity not in {str(value) for value in job_ids}:
                raise RuntimeError("campaign lane status identities differ")
            if state not in allowed:
                raise RuntimeError("campaign lane status is invalid")
            states[identity] = state
        return states


def _exact_ids(values: Sequence[JobId]) -> list[str]:
    ids = [str(value) for value in values]
    if not 1 <= len(ids) <= 72 or len(set(ids)) != len(ids):
        raise ValueError("campaign claim requires 1..72 unique exact identities")
    return ids


def _stage_job_ids(
    window: DeferredAnalysisWindowV1, stage: DeferredAnalysisStage
) -> tuple[JobId, ...]:
    if stage in {
        DeferredAnalysisStage.FEATURE_COMPUTE,
        DeferredAnalysisStage.FEATURE_PROJECTION,
    }:
        return window.feature_job_ids
    if stage in {
        DeferredAnalysisStage.WATERFALL_COMPUTE,
        DeferredAnalysisStage.WATERFALL_PROJECTION,
    }:
        return window.waterfall_job_ids
    return window.starlink_suite_job_ids


def _job_lease(row: dict[str, object]) -> JobLease:
    expires = row.get("lease_expires_utc")
    payload = row.get("payload")
    if not isinstance(expires, datetime) or not isinstance(payload, dict):
        raise TypeError("campaign-scoped job lease row is malformed")
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    normalized = expires.astimezone(UTC)
    delta = normalized - epoch
    expires_ns = (
        delta.days * 86_400 + delta.seconds
    ) * 1_000_000_000 + delta.microseconds * 1_000
    attempt_value = row.get("attempt")
    generation_value = row.get("lease_generation")
    if (
        isinstance(attempt_value, bool)
        or not isinstance(attempt_value, int)
        or isinstance(generation_value, bool)
        or not isinstance(generation_value, int)
    ):
        raise TypeError("campaign-scoped job lease row is malformed")
    try:
        token = str(row["lease_token"])
        return JobLease(
            JobId(str(row["job_id"])),
            JobType(str(row["job_type"])),
            JobPayload.create(
                SchemaRef(
                    str(row["payload_schema_id"]),
                    SchemaVersion.parse(str(row["payload_schema_version"])),
                ),
                payload,
            ),
            attempt_value,
            token,
            generation_value,
            UtcNs(expires_ns),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("campaign-scoped job lease row is malformed") from error
