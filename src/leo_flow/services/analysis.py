"""Job-driven analysis boundary with no capture implementation dependency."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from leo_flow.contracts.core import ArtifactRef
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.jobs.ports import JobLeaseRepository

from .config import AnalysisServiceConfig
from .lifecycle import DiagnosticSink, ServiceLoop


class AnalysisCycle(Protocol):
    """Resolve one leased job through injected readers and publishers."""

    def preflight(self) -> None: ...

    def process_one_job(self) -> bool: ...

    def close(self, timeout_s: float) -> None: ...


class AnalysisJobProcessor(Protocol):
    """Resolve lease refs through readers and publish the resulting artifact."""

    def process(self, lease: JobLease) -> ArtifactRef: ...


class FencedAnalysisCycle:
    """One fenced recording/model analysis lease per service iteration."""

    def __init__(
        self,
        repository: JobLeaseRepository,
        processor: AnalysisJobProcessor,
        *,
        worker_id: str,
        lease_ttl_s: float,
        preflight: Callable[[], None] = lambda: None,
        close: Callable[[float], None] = lambda timeout_s: None,
    ) -> None:
        if not worker_id or lease_ttl_s <= 0:
            raise ValueError("worker identity and positive lease TTL are required")
        self._repository = repository
        self._processor = processor
        self._worker_id = worker_id
        self._lease_ttl_s = lease_ttl_s
        self._preflight = preflight
        self._close = close

    def preflight(self) -> None:
        self._preflight()

    def process_one_job(self) -> bool:
        lease = self._repository.claim(
            (JobType.RECORDING_ANALYSIS, JobType.MODEL_ANALYSIS),
            self._worker_id,
            self._lease_ttl_s,
        )
        if lease is None:
            return False
        try:
            result = self._processor.process(lease)
        except Exception as error:
            self._repository.fail(
                lease.job_id,
                lease.lease_token,
                lease.lease_generation,
                f"{type(error).__name__}: processor failed",
                None,
            )
            raise
        self._repository.complete(
            lease.job_id,
            lease.lease_token,
            lease.lease_generation,
            result,
        )
        return True

    def close(self, timeout_s: float) -> None:
        self._close(timeout_s)


def build_analysis_service(
    config: AnalysisServiceConfig,
    cycle: AnalysisCycle,
    *,
    diagnostics: DiagnosticSink | None = None,
) -> ServiceLoop:
    return ServiceLoop(
        service="analysis",
        instance_id=config.runtime.instance_id,
        start=cycle.preflight,
        step=cycle.process_one_job,
        close=cycle.close,
        poll_interval_s=config.runtime.poll_interval_s,
        shutdown_timeout_s=config.runtime.shutdown_timeout_s,
        diagnostics=diagnostics,
    )
