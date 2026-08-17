"""Durable, replay-safe projection of bounded waterfall artifacts."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts._validation import require_positive, require_token
from leo_flow.contracts.core import JobId
from leo_flow.contracts.waterfall import (
    WaterfallBundleV0_1,
    WaterfallProductRefV0_1,
)


@dataclass(frozen=True)
class WaterfallProjectionLeaseV0_1:
    work_id: str
    source_job_id: JobId
    waterfall_ref: WaterfallProductRefV0_1
    lease_token: str
    lease_generation: int
    attempt: int

    def __post_init__(self) -> None:
        require_token(self.work_id, "work_id")
        if not self.work_id.startswith("wfwork_"):
            raise ValueError("waterfall work ID is invalid")
        require_token(self.lease_token, "lease_token")
        require_positive(self.lease_generation, "lease_generation")
        require_positive(self.attempt, "attempt")


class WaterfallProjectionWorkRepositoryV0_1(Protocol):
    def claim(
        self, worker_id: str, lease_ttl_s: float
    ) -> WaterfallProjectionLeaseV0_1 | None: ...

    def complete(self, lease: WaterfallProjectionLeaseV0_1) -> None: ...

    def retry(
        self, lease: WaterfallProjectionLeaseV0_1, reason: str, delay_s: float
    ) -> None: ...

    def park(self, lease: WaterfallProjectionLeaseV0_1, reason: str) -> None: ...


class WaterfallViewV0_1(Protocol):
    @property
    def ref(self) -> WaterfallProductRefV0_1: ...

    def bundle(self) -> WaterfallBundleV0_1: ...


class WaterfallReaderV0_1(Protocol):
    def open(
        self, ref: WaterfallProductRefV0_1
    ) -> AbstractContextManager[WaterfallViewV0_1]: ...


class WaterfallDashboardProjectionWriterV0_1(Protocol):
    def project_complete(
        self, bundle: WaterfallBundleV0_1, ref: WaterfallProductRefV0_1
    ) -> int: ...


class WaterfallDashboardProjectionWorkerV0_1:
    """Resolve one exact artifact, project it, then fence the work transition."""

    def __init__(
        self,
        work: WaterfallProjectionWorkRepositoryV0_1,
        waterfalls: WaterfallReaderV0_1,
        writer: WaterfallDashboardProjectionWriterV0_1,
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
        self._waterfalls = waterfalls
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

    def execute(self, lease: WaterfallProjectionLeaseV0_1) -> None:
        try:
            with self._waterfalls.open(lease.waterfall_ref) as view:
                if view.ref != lease.waterfall_ref:
                    raise ValueError(
                        "waterfall reader returned a different public reference"
                    )
                bundle = view.bundle()
            if (
                bundle.product_id != lease.waterfall_ref.product_id
                or bundle.analysis_run_id != lease.waterfall_ref.analysis_run_id
                or bundle.recording_id != lease.waterfall_ref.recording_id
            ):
                raise ValueError(
                    "waterfall bundle and projection work identities differ"
                )
            self._writer.project_complete(bundle, lease.waterfall_ref)
            self._work.complete(lease)
        except ValueError:
            self._park(lease, "waterfall-projection-identity-mismatch")
        except Exception:  # noqa: BLE001 - bounded durable retry boundary
            if lease.attempt >= self._maximum_attempts:
                self._park(lease, "waterfall-projection-attempts-exhausted")
            else:
                self._work.retry(
                    lease,
                    "waterfall-projection-transient-failure",
                    self._retry_delay_s,
                )

    def _park(self, lease: WaterfallProjectionLeaseV0_1, reason: str) -> None:
        self._work.park(lease, reason)
