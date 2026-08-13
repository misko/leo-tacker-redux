"""Job repository capability, including generation fencing on every mutation."""

from __future__ import annotations

from typing import Protocol

from leo_flow.contracts.core import ArtifactRef, JobId, UtcNs

from .contracts import JobLease, JobType


class JobLeaseRepository(Protocol):
    def claim(
        self,
        types: tuple[JobType, ...],
        worker_id: str,
        ttl_s: float,
    ) -> JobLease | None: ...

    def heartbeat(
        self,
        job_id: JobId,
        lease_token: str,
        generation: int,
        ttl_s: float,
    ) -> JobLease: ...

    def complete(
        self,
        job_id: JobId,
        lease_token: str,
        generation: int,
        result_ref: ArtifactRef,
    ) -> None: ...

    def fail(
        self,
        job_id: JobId,
        lease_token: str,
        generation: int,
        reason: str,
        retry_at_utc_ns: UtcNs | None,
    ) -> None: ...
