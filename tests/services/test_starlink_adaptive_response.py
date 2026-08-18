from __future__ import annotations

from dataclasses import dataclass

from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    RadioId,
    ReceiverChainId,
    SchemaRef,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_full_dwell_timeline_product import (
    V0_1,
    FullDwellRefinementRequestV0_1,
    FullDwellRefinementWindowV0_1,
)
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteProductRefV0_2,
)
from leo_flow.services.starlink_adaptive_response import (
    AdaptiveResponseWorkLeaseV0_1,
    BoundedAdaptiveResponseServiceV0_1,
    StaleAdaptiveResponseLeaseError,
)
from tests.recording_analysis.fakes import SegmentFixture, make_view


def _lease(attempt: int = 1) -> AdaptiveResponseWorkLeaseV0_1:
    _view, recording = make_view(SegmentFixture(bytes(8 * 100), 2_500_000))
    segment_id = _view.manifest.segments[0].segment_id
    timeline = ArtifactRef("fdtl_" + "1" * 32, Digest.sha256(b"timeline"), None)
    refinement = FullDwellRefinementRequestV0_1(
        SchemaRef(FullDwellRefinementRequestV0_1.SCHEMA_ID, V0_1),
        recording.recording_id,
        recording,
        timeline,
        Digest.sha256(b"timeline-request"),
        (
            FullDwellRefinementWindowV0_1(
                RadioId("radio_synthetic"),
                "lnb-current-a",
                segment_id,
                ReceiverChainId("rx_0"),
                4,
                StarlinkEdge.LOWER,
                0,
                0,
                20,
            ),
        ),
        "top-power-then-start;pattern-blind-per-stream",
    )
    return AdaptiveResponseWorkLeaseV0_1(
        refinement,
        StarlinkDetectorSuiteProductRefV0_2(
            "slsuite_" + "2" * 32,
            recording.recording_id,
            recording.metadata_object,
        ),
        Digest.sha256(b"suite-request"),
        "lease-token",
        1,
        attempt,
    )


@dataclass
class _Work:
    lease: AdaptiveResponseWorkLeaseV0_1 | None
    event: str = ""

    def claim(self, worker_id: str, lease_ttl_s: float):
        assert worker_id == "worker-a" and lease_ttl_s == 90
        result, self.lease = self.lease, None
        return result

    def complete(self, lease, result):
        self.event = f"complete:{result.artifact_id}"

    def retry(self, lease, reason: str):
        self.event = f"retry:{reason}"


class _Producer:
    def __init__(self, result: object) -> None:
        self.result = result

    def produce(self, lease):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_optional_worker_completes_and_never_drives_capture() -> None:
    work = _Work(_lease())
    result = ArtifactRef("slar_" + "3" * 32, Digest.sha256(b"result"), None)
    service = BoundedAdaptiveResponseServiceV0_1(
        work, _Producer(result), worker_id="worker-a", lease_ttl_s=90
    )
    assert service.run_once()
    assert work.event == f"complete:{result.artifact_id}"
    assert not service.run_once()


def test_optional_worker_retries_invalid_and_exhausted_work() -> None:
    invalid = _Work(_lease())
    BoundedAdaptiveResponseServiceV0_1(
        invalid, _Producer(ValueError("bad")), worker_id="worker-a", lease_ttl_s=90
    ).run_once()
    assert invalid.event == "retry:adaptive-response-invalid-input"

    exhausted = _Work(_lease(3))
    BoundedAdaptiveResponseServiceV0_1(
        exhausted,
        _Producer(RuntimeError("transient")),
        worker_id="worker-a",
        lease_ttl_s=90,
        maximum_attempts=3,
    ).run_once()
    assert exhausted.event == "retry:adaptive-response-attempts-exhausted"


def test_stale_completion_does_not_retry_or_mutate_another_lease() -> None:
    work = _Work(_lease())
    result = ArtifactRef("slar_" + "4" * 32, Digest.sha256(b"result"), None)

    def stale(_lease, _result):
        raise StaleAdaptiveResponseLeaseError("stale")

    work.complete = stale  # type: ignore[method-assign]
    BoundedAdaptiveResponseServiceV0_1(
        work, _Producer(result), worker_id="worker-a", lease_ttl_s=90
    ).run_once()
    assert work.event == ""
