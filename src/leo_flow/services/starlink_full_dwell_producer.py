"""Fenced, bounded producer for optional full-dwell response products."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import ArtifactRef, Digest, RecordingId, UtcNs
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteProductRefV0_2,
)


class StaleFullDwellWorkLeaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class FullDwellWorkLeaseV0_1:
    source_suite_ref: StarlinkDetectorSuiteProductRefV0_2
    source_suite_request_digest: Digest
    lease_token: str
    lease_generation: int
    attempt: int


@dataclass(frozen=True)
class FullDwellAdmissionResultV0_1:
    admitted: int
    active_backlog: int
    saturated: bool

    def __post_init__(self) -> None:
        if self.admitted < 0 or self.active_backlog < 0:
            raise ValueError("full-dwell admission counts cannot be negative")


class FullDwellWorkRepositoryV0_1(Protocol):
    def admit(
        self, *, maximum_new: int, maximum_active: int
    ) -> FullDwellAdmissionResultV0_1: ...

    def claim(
        self, worker_id: str, lease_ttl_s: float
    ) -> FullDwellWorkLeaseV0_1 | None: ...

    def complete(self, lease: FullDwellWorkLeaseV0_1, result: ArtifactRef) -> None: ...

    def retry(
        self, lease: FullDwellWorkLeaseV0_1, reason: str, retry_at_utc_ns: UtcNs
    ) -> None: ...

    def park(self, lease: FullDwellWorkLeaseV0_1, reason: str) -> None: ...


class FullDwellLeaseProducerV0_1(Protocol):
    def produce(self, lease: FullDwellWorkLeaseV0_1) -> ArtifactRef: ...


class BoundedFullDwellProducerServiceV0_1:
    """Admit and execute bounded optional work without a capture dependency."""

    def __init__(
        self,
        work: FullDwellWorkRepositoryV0_1,
        producer: FullDwellLeaseProducerV0_1,
        *,
        worker_id: str,
        maximum_active: int = 8,
        maximum_admissions_per_cycle: int = 2,
        lease_ttl_s: float = 7200.0,
        maximum_attempts: int = 3,
        retry_delay_s: float = 30.0,
        clock_ns: ProtocolClock | None = None,
    ) -> None:
        if (
            not worker_id
            or not 1 <= maximum_active <= 256
            or not 1 <= maximum_admissions_per_cycle <= maximum_active
            or lease_ttl_s <= 0
            or maximum_attempts <= 0
            or retry_delay_s <= 0
        ):
            raise ValueError("full-dwell producer bounds are invalid")
        self._work = work
        self._producer = producer
        self._worker_id = worker_id
        self._maximum_active = maximum_active
        self._maximum_admissions = maximum_admissions_per_cycle
        self._lease_ttl_s = lease_ttl_s
        self._maximum_attempts = maximum_attempts
        self._retry_delay_ns = round(retry_delay_s * 1_000_000_000)
        self._clock_ns = clock_ns or time.time_ns

    def run_once(self) -> tuple[FullDwellAdmissionResultV0_1, bool]:
        admission = self._work.admit(
            maximum_new=self._maximum_admissions,
            maximum_active=self._maximum_active,
        )
        lease = self._work.claim(self._worker_id, self._lease_ttl_s)
        if lease is None:
            return admission, False
        try:
            result = self._producer.produce(lease)
            self._work.complete(lease, result)
        except StaleFullDwellWorkLeaseError:
            pass
        except ValueError:
            self._safe_park(lease, "full-dwell-invalid-input")
        except Exception:  # noqa: BLE001 - durable retry boundary
            if lease.attempt >= self._maximum_attempts:
                self._safe_park(lease, "full-dwell-attempts-exhausted")
            else:
                try:
                    self._work.retry(
                        lease,
                        "full-dwell-transient-failure",
                        UtcNs(self._clock_ns() + self._retry_delay_ns),
                    )
                except StaleFullDwellWorkLeaseError:
                    pass
        return admission, True

    def _safe_park(self, lease: FullDwellWorkLeaseV0_1, reason: str) -> None:
        try:
            self._work.park(lease, reason)
        except StaleFullDwellWorkLeaseError:
            pass


class ProtocolClock(Protocol):
    def __call__(self) -> int: ...


def deterministic_full_dwell_work_identity(
    recording_id: RecordingId, source_analysis_id: str, source_request_digest: Digest
) -> str:
    """Human-inspectable exact identity used only for logs/tests."""
    return f"{recording_id}:{source_analysis_id}:{source_request_digest.value}"
