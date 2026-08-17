"""Replay-safe projection of durable Starlink candidate bundles."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts._validation import require_positive, require_token
from leo_flow.contracts.core import JobId
from leo_flow.contracts.starlink import StarlinkPilotAnalysisBundleV0_1
from leo_flow.contracts.starlink_pipeline import StarlinkPilotAnalysisProductRefV0_1


@dataclass(frozen=True)
class StarlinkProjectionLeaseV0_1:
    work_id: str
    source_job_id: JobId
    product_ref: StarlinkPilotAnalysisProductRefV0_1
    lease_token: str
    lease_generation: int
    attempt: int

    def __post_init__(self) -> None:
        require_token(self.work_id, "work_id")
        if not self.work_id.startswith("slwork_"):
            raise ValueError("Starlink work identity is invalid")
        require_token(self.lease_token, "lease_token")
        require_positive(self.lease_generation, "lease_generation")
        require_positive(self.attempt, "attempt")


class StarlinkProjectionWorkRepositoryV0_1(Protocol):
    def claim(
        self, worker_id: str, lease_ttl_s: float
    ) -> StarlinkProjectionLeaseV0_1 | None: ...
    def complete(self, lease: StarlinkProjectionLeaseV0_1) -> None: ...
    def retry(
        self, lease: StarlinkProjectionLeaseV0_1, reason: str, delay_s: float
    ) -> None: ...
    def park(self, lease: StarlinkProjectionLeaseV0_1, reason: str) -> None: ...


class StarlinkViewV0_1(Protocol):
    @property
    def ref(self) -> StarlinkPilotAnalysisProductRefV0_1: ...
    def bundle(self) -> StarlinkPilotAnalysisBundleV0_1: ...


class StarlinkReaderV0_1(Protocol):
    def open(
        self, ref: StarlinkPilotAnalysisProductRefV0_1
    ) -> AbstractContextManager[StarlinkViewV0_1]: ...


class StarlinkDashboardProjectionWriterV0_1(Protocol):
    def project_candidates(
        self,
        bundle: StarlinkPilotAnalysisBundleV0_1,
        ref: StarlinkPilotAnalysisProductRefV0_1,
        lease: StarlinkProjectionLeaseV0_1,
    ) -> int: ...


class StarlinkDashboardProjectionWorkerV0_1:
    def __init__(
        self,
        work: StarlinkProjectionWorkRepositoryV0_1,
        results: StarlinkReaderV0_1,
        writer: StarlinkDashboardProjectionWriterV0_1,
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
        self._work = work
        self._results = results
        self._writer = writer
        self._worker_id = worker_id
        self._lease_ttl_s = lease_ttl_s
        self._maximum_attempts = maximum_attempts
        self._retry_delay_s = retry_delay_s

    def process_one_work(self) -> bool:
        lease = self._work.claim(self._worker_id, self._lease_ttl_s)
        if lease is None:
            return False
        self.execute(lease)
        return True

    def execute(self, lease: StarlinkProjectionLeaseV0_1) -> None:
        try:
            with self._results.open(lease.product_ref) as view:
                if view.ref != lease.product_ref:
                    raise ValueError("Starlink reader returned another reference")
                bundle = view.bundle()
            if (
                bundle.analysis_id != lease.product_ref.analysis_id
                or bundle.recording_id != lease.product_ref.recording_id
            ):
                raise ValueError("Starlink bundle and work identities differ")
            self._writer.project_candidates(bundle, lease.product_ref, lease)
            self._work.complete(lease)
        except ValueError:
            self._work.park(lease, "starlink-projection-identity-mismatch")
        except Exception:  # noqa: BLE001
            if lease.attempt >= self._maximum_attempts:
                self._work.park(lease, "starlink-projection-attempts-exhausted")
            else:
                self._work.retry(
                    lease, "starlink-projection-transient-failure", self._retry_delay_s
                )
