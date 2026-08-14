"""Typed analysis routing without cross-capability processing logic."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Protocol

from leo_flow.contracts.core import UtcNs
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.jobs.ports import JobLeaseRepository


class AnalysisLeaseExecutor(Protocol):
    """One capability-scoped executor that owns its terminal job mutation."""

    def execute(self, lease: JobLease) -> object: ...


class UnsupportedAnalysisJobError(RuntimeError):
    pass


class EphemerisLinkBackfillUnavailable:
    """Fail one claimed backfill lease with a stable, non-secret reason code."""

    REASON = "ephemeris-link-backfill-not-implemented"
    # PostgreSQL's maximum finite timestamp, so an unavailable capability does
    # not hot-loop. Enabling it requires an explicit operator requeue.
    RETRY_AT_UTC_NS = UtcNs(253_402_300_799_000_000_000)

    def __init__(self, jobs: JobLeaseRepository) -> None:
        self._jobs = jobs

    def execute(self, lease: JobLease) -> None:
        if lease.job_type is not JobType.EPHEMERIS_LINK_BACKFILL:
            raise UnsupportedAnalysisJobError(
                "backfill executor received a different job type"
            )
        self._jobs.fail(
            lease.job_id,
            lease.lease_token,
            lease.lease_generation,
            self.REASON,
            self.RETRY_AT_UTC_NS,
        )
        raise UnsupportedAnalysisJobError(self.REASON)


class TypedAnalysisRouterCycle:
    """Claim all declared analysis jobs and dispatch by exact enum identity.

    Executors retain preparation, retry-policy, atomic publication, and fenced
    completion ownership. The router never publishes or completes artifacts.
    """

    CLAIMED_TYPES = (
        JobType.RECORDING_ANALYSIS,
        JobType.MODEL_ANALYSIS,
        JobType.EPHEMERIS_RETRIEVAL,
        JobType.EPHEMERIS_LINK_BACKFILL,
    )

    def __init__(
        self,
        jobs: JobLeaseRepository,
        *,
        recording_analysis: AnalysisLeaseExecutor,
        model_analysis: AnalysisLeaseExecutor,
        ephemeris_retrieval: AnalysisLeaseExecutor,
        ephemeris_link_backfill: AnalysisLeaseExecutor,
        worker_id: str,
        lease_ttl_s: float,
        preflight: Callable[[], None] = lambda: None,
        close: Callable[[float], None] = lambda timeout_s: None,
    ) -> None:
        if not worker_id or lease_ttl_s <= 0:
            raise ValueError("worker identity and positive lease TTL are required")
        self._jobs = jobs
        self._executors: Mapping[JobType, AnalysisLeaseExecutor] = MappingProxyType(
            {
                JobType.RECORDING_ANALYSIS: recording_analysis,
                JobType.MODEL_ANALYSIS: model_analysis,
                JobType.EPHEMERIS_RETRIEVAL: ephemeris_retrieval,
                JobType.EPHEMERIS_LINK_BACKFILL: ephemeris_link_backfill,
            }
        )
        self._worker_id = worker_id
        self._lease_ttl_s = lease_ttl_s
        self._preflight = preflight
        self._close = close

    def preflight(self) -> None:
        self._preflight()

    def process_one_job(self) -> bool:
        lease = self._jobs.claim(self.CLAIMED_TYPES, self._worker_id, self._lease_ttl_s)
        if lease is None:
            return False
        executor = self._executors[lease.job_type]
        executor.execute(lease)
        return True

    def close(self, timeout_s: float) -> None:
        self._close(timeout_s)
