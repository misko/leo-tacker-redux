"""Replay-safe projection of durable v0.2 Starlink detector-suite results."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts._validation import require_positive, require_token
from leo_flow.contracts.core import JobId
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteProductRefV0_2,
    StarlinkDetectorSuiteRecordingBundleV0_2,
)


@dataclass(frozen=True)
class StarlinkSuiteProjectionLeaseV0_2:
    work_id: str
    source_job_id: JobId
    product_ref: StarlinkDetectorSuiteProductRefV0_2
    lease_token: str
    lease_generation: int
    attempt: int

    def __post_init__(self) -> None:
        require_token(self.work_id, "work_id")
        require_token(self.lease_token, "lease_token")
        if not self.work_id.startswith("slsuitework_"):
            raise ValueError("invalid detector-suite work identity")
        require_positive(self.lease_generation, "lease_generation")
        require_positive(self.attempt, "attempt")


class StarlinkSuiteProjectionWorkRepositoryV0_2(Protocol):
    def claim(
        self, worker_id: str, lease_ttl_s: float
    ) -> StarlinkSuiteProjectionLeaseV0_2 | None: ...
    def complete(self, lease: StarlinkSuiteProjectionLeaseV0_2) -> None: ...
    def retry(
        self, lease: StarlinkSuiteProjectionLeaseV0_2, reason: str, delay_s: float
    ) -> None: ...
    def park(self, lease: StarlinkSuiteProjectionLeaseV0_2, reason: str) -> None: ...


class StarlinkSuiteReaderV0_2(Protocol):
    def open(
        self, ref: StarlinkDetectorSuiteProductRefV0_2
    ) -> AbstractContextManager[StarlinkDetectorSuiteRecordingBundleV0_2]: ...


class StarlinkSuiteDashboardProjectionWriterV0_2(Protocol):
    def project_suite(
        self,
        bundle: StarlinkDetectorSuiteRecordingBundleV0_2,
        ref: StarlinkDetectorSuiteProductRefV0_2,
        lease: StarlinkSuiteProjectionLeaseV0_2,
    ) -> int: ...


class StarlinkSuiteDashboardProjectionWorkerV0_2:
    def __init__(
        self,
        work: StarlinkSuiteProjectionWorkRepositoryV0_2,
        results: StarlinkSuiteReaderV0_2,
        writer: StarlinkSuiteDashboardProjectionWriterV0_2,
        *,
        worker_id: str,
        lease_ttl_s: float,
        maximum_attempts: int = 3,
        retry_delay_s: float = 5.0,
    ) -> None:
        require_token(worker_id, "worker_id")
        require_positive(lease_ttl_s, "lease_ttl_s")
        require_positive(maximum_attempts, "maximum_attempts")
        require_positive(retry_delay_s, "retry_delay_s")
        self._work, self._results, self._writer = work, results, writer
        self._worker_id, self._lease_ttl_s = worker_id, lease_ttl_s
        self._maximum_attempts, self._retry_delay_s = maximum_attempts, retry_delay_s

    def process_one_work(self) -> bool:
        lease = self._work.claim(self._worker_id, self._lease_ttl_s)
        if lease is None:
            return False
        self.execute(lease)
        return True

    def execute(self, lease: StarlinkSuiteProjectionLeaseV0_2) -> None:
        try:
            with self._results.open(lease.product_ref) as bundle:
                if (
                    bundle.analysis_id != lease.product_ref.analysis_id
                    or bundle.recording_id != lease.product_ref.recording_id
                ):
                    raise ValueError("detector-suite bundle and work differ")
            self._writer.project_suite(bundle, lease.product_ref, lease)
            self._work.complete(lease)
        except ValueError:
            self._work.park(lease, "starlink-suite-projection-identity-mismatch")
        except Exception:  # noqa: BLE001
            if lease.attempt >= self._maximum_attempts:
                self._work.park(lease, "starlink-suite-projection-attempts-exhausted")
            else:
                self._work.retry(
                    lease,
                    "starlink-suite-projection-transient-failure",
                    self._retry_delay_s,
                )
