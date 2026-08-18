from __future__ import annotations

from dataclasses import dataclass

from leo_flow.contracts.core import ArtifactRef, Digest, RecordingId, SchemaRef, UtcNs
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteProductRefV0_2,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.services.starlink_full_dwell_producer import (
    BoundedFullDwellProducerServiceV0_1,
    FullDwellAdmissionResultV0_1,
    FullDwellWorkLeaseV0_1,
    StaleFullDwellWorkLeaseError,
)


def _lease(attempt: int = 1) -> FullDwellWorkLeaseV0_1:
    blob = ObjectRef(
        Digest.sha256(b"suite"), 5, "application/json", "suite", "cas:suite"
    )
    return FullDwellWorkLeaseV0_1(
        StarlinkDetectorSuiteProductRefV0_2(
            "slsuite_" + "1" * 32, RecordingId("rec_fd"), blob
        ),
        Digest.sha256(b"request"),
        "lease_1",
        1,
        attempt,
    )


class Work:
    def __init__(self, lease: FullDwellWorkLeaseV0_1 | None) -> None:
        self.lease = lease
        self.calls: list[tuple[object, ...]] = []

    def admit(self, *, maximum_new: int, maximum_active: int):
        self.calls.append(("admit", maximum_new, maximum_active))
        return FullDwellAdmissionResultV0_1(1, 1, False)

    def claim(self, worker_id: str, lease_ttl_s: float):
        self.calls.append(("claim", worker_id, lease_ttl_s))
        return self.lease

    def complete(self, lease, result):
        self.calls.append(("complete", lease, result))

    def retry(self, lease, reason, retry_at_utc_ns):
        self.calls.append(("retry", lease, reason, retry_at_utc_ns))

    def park(self, lease, reason):
        self.calls.append(("park", lease, reason))


class StaleCompleteWork(Work):
    def complete(self, lease, result):
        raise StaleFullDwellWorkLeaseError("expired")


@dataclass
class Producer:
    error: Exception | None = None

    def produce(self, lease: FullDwellWorkLeaseV0_1) -> ArtifactRef:
        if self.error is not None:
            raise self.error
        return ArtifactRef(
            "slfd_" + "2" * 32, Digest.sha256(b"result"), SchemaRef("fd")
        )


def test_cycle_admits_with_hard_bounds_then_fenced_completes() -> None:
    work = Work(_lease())
    admission, progressed = BoundedFullDwellProducerServiceV0_1(
        work,
        Producer(),
        worker_id="worker",
        maximum_active=8,
        maximum_admissions_per_cycle=2,
    ).run_once()
    assert progressed and admission.admitted == 1
    assert work.calls[0] == ("admit", 2, 8)
    assert work.calls[1] == ("claim", "worker", 7200.0)
    assert work.calls[-1][0] == "complete"


def test_empty_cycle_is_nonblocking_and_does_not_execute() -> None:
    work = Work(None)
    _, progressed = BoundedFullDwellProducerServiceV0_1(
        work, Producer(), worker_id="worker"
    ).run_once()
    assert not progressed
    assert [call[0] for call in work.calls] == ["admit", "claim"]


def test_transient_failure_retries_at_bounded_delay() -> None:
    work = Work(_lease())
    BoundedFullDwellProducerServiceV0_1(
        work, Producer(RuntimeError("secret")), worker_id="worker", clock_ns=lambda: 100
    ).run_once()
    retry = work.calls[-1]
    assert retry[:3] == ("retry", _lease(), "full-dwell-transient-failure")
    assert retry[3] == UtcNs(30_000_000_100)


def test_invalid_or_exhausted_work_parks_with_sanitized_reason() -> None:
    invalid = Work(_lease())
    BoundedFullDwellProducerServiceV0_1(
        invalid, Producer(ValueError("bad")), worker_id="worker"
    ).run_once()
    assert invalid.calls[-1][0:] == ("park", _lease(), "full-dwell-invalid-input")

    exhausted = Work(_lease(3))
    BoundedFullDwellProducerServiceV0_1(
        exhausted, Producer(RuntimeError("bad")), worker_id="worker"
    ).run_once()
    assert exhausted.calls[-1][0:] == (
        "park",
        _lease(3),
        "full-dwell-attempts-exhausted",
    )


def test_stale_completion_does_not_attempt_a_second_transition() -> None:
    work = StaleCompleteWork(_lease())
    _, progressed = BoundedFullDwellProducerServiceV0_1(
        work, Producer(), worker_id="worker"
    ).run_once()
    assert progressed
    assert [call[0] for call in work.calls] == ["admit", "claim"]
