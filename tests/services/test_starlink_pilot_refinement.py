from __future__ import annotations

from dataclasses import dataclass

from leo_flow.contracts.core import ArtifactRef, Digest
from leo_flow.contracts.starlink_pilot_prescreen import (
    StarlinkPilotPrescreenProductRefV0_1,
)
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteProductRefV0_2,
)
from leo_flow.services.starlink_pilot_refinement import (
    BoundedPilotRefinementServiceV0_1,
    PilotRefinementWorkLeaseV0_1,
    StalePilotRefinementLeaseError,
)
from tests.recording_analysis.fakes import SegmentFixture, make_view


def _lease(attempt: int = 1) -> PilotRefinementWorkLeaseV0_1:
    _view, recording = make_view(SegmentFixture(bytes(8 * 100), 2_500_000))
    return PilotRefinementWorkLeaseV0_1(
        "slprw_" + "1" * 32,
        recording,
        StarlinkPilotPrescreenProductRefV0_1(
            "slps_" + "2" * 32, recording.recording_id, recording.metadata_object
        ),
        StarlinkDetectorSuiteProductRefV0_2(
            "slsuite_" + "3" * 32,
            recording.recording_id,
            recording.data_object,
        ),
        Digest.sha256(b"suite-request"),
        "lease-token",
        1,
        attempt,
    )


@dataclass
class _Work:
    lease: PilotRefinementWorkLeaseV0_1 | None
    event: str = ""

    def claim(self, worker_id: str, lease_ttl_s: float):  # type: ignore[no-untyped-def]
        assert worker_id == "worker-a" and lease_ttl_s == 90
        result, self.lease = self.lease, None
        return result

    def complete(self, lease, result):  # type: ignore[no-untyped-def]
        del lease
        self.event = f"complete:{result.artifact_id}"

    def retry(self, lease, reason: str):  # type: ignore[no-untyped-def]
        del lease
        self.event = f"retry:{reason}"


class _Producer:
    def __init__(self, result: ArtifactRef | Exception) -> None:
        self.result = result

    def produce(self, lease: PilotRefinementWorkLeaseV0_1) -> ArtifactRef:
        del lease
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_optional_refinement_worker_completes_without_capture_dependency() -> None:
    work = _Work(_lease())
    result = ArtifactRef("slpr_" + "4" * 32, Digest.sha256(b"result"), None)
    service = BoundedPilotRefinementServiceV0_1(
        work, _Producer(result), worker_id="worker-a", lease_ttl_s=90
    )

    assert service.run_once()
    assert work.event == f"complete:{result.artifact_id}"
    assert not service.run_once()


def test_optional_refinement_worker_retries_and_bounds_attempts() -> None:
    transient = _Work(_lease())
    BoundedPilotRefinementServiceV0_1(
        transient,
        _Producer(RuntimeError("transient")),
        worker_id="worker-a",
        lease_ttl_s=90,
    ).run_once()
    assert transient.event == "retry:pilot-refinement-transient-failure"

    exhausted = _Work(_lease(3))
    BoundedPilotRefinementServiceV0_1(
        exhausted,
        _Producer(RuntimeError("transient")),
        worker_id="worker-a",
        lease_ttl_s=90,
        maximum_attempts=3,
    ).run_once()
    assert exhausted.event == "retry:pilot-refinement-attempts-exhausted"


def test_stale_refinement_completion_never_retries_another_lease() -> None:
    work = _Work(_lease())
    result = ArtifactRef("slpr_" + "5" * 32, Digest.sha256(b"result"), None)

    def stale(_lease, _result):  # type: ignore[no-untyped-def]
        raise StalePilotRefinementLeaseError("stale")

    work.complete = stale  # type: ignore[method-assign]
    BoundedPilotRefinementServiceV0_1(
        work, _Producer(result), worker_id="worker-a", lease_ttl_s=90
    ).run_once()
    assert work.event == ""
