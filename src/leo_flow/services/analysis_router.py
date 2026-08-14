"""Typed analysis routing without cross-capability processing logic."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Protocol

from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.jobs.ports import JobLeaseRepository


class AnalysisLeaseExecutor(Protocol):
    """One capability-scoped executor that owns its terminal job mutation."""

    def execute(self, lease: JobLease) -> object: ...


class TypedAnalysisRouterCycle:
    """Claim only installed analysis capabilities and dispatch by exact identity.

    Executors retain preparation, retry-policy, atomic publication, and fenced
    completion ownership. The router never publishes or completes artifacts.
    """

    def __init__(
        self,
        jobs: JobLeaseRepository,
        *,
        executors: Mapping[JobType, AnalysisLeaseExecutor],
        worker_id: str,
        lease_ttl_s: float,
        preflight: Callable[[], None] = lambda: None,
        close: Callable[[float], None] = lambda timeout_s: None,
    ) -> None:
        if not worker_id or lease_ttl_s <= 0 or not executors:
            raise ValueError(
                "worker identity, positive lease TTL, and executors are required"
            )
        self._jobs = jobs
        self._executors: Mapping[JobType, AnalysisLeaseExecutor] = MappingProxyType(
            dict(executors)
        )
        if not all(callable(executor.execute) for executor in self._executors.values()):
            raise TypeError("every analysis executor must provide execute")
        self._claimed_types = tuple(
            sorted(self._executors, key=lambda kind: kind.value)
        )
        self._worker_id = worker_id
        self._lease_ttl_s = lease_ttl_s
        self._preflight = preflight
        self._close = close

    def preflight(self) -> None:
        self._preflight()

    def process_one_job(self) -> bool:
        lease = self._jobs.claim(
            self._claimed_types, self._worker_id, self._lease_ttl_s
        )
        if lease is None:
            return False
        executor = self._executors[lease.job_type]
        executor.execute(lease)
        return True

    @property
    def claimed_types(self) -> tuple[JobType, ...]:
        return self._claimed_types

    def close(self, timeout_s: float) -> None:
        self._close(timeout_s)
