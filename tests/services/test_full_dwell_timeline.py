from __future__ import annotations

from dataclasses import dataclass

from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    SegmentId,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_full_dwell_timeline_product import (
    V0_1,
    FullDwellRefinementRequestV0_1,
    FullDwellRefinementWindowV0_1,
    FullDwellTimelinePlanV0_1,
    FullDwellTimelineStreamSelectionV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.services.full_dwell_timeline import (
    FullDwellTimelineLeaseV0_1,
    IndependentFullDwellTimelineServiceV0_1,
    ProducedFullDwellTimelineV0_1,
)


def _result() -> ProducedFullDwellTimelineV0_1:
    recording_id = RecordingId("rec_timeline")
    digest = Digest.sha256(b"timeline")
    data_ref = ObjectRef(digest, 8, "application/octet-stream", "test", "cas:data")
    metadata_digest = Digest.sha256(b"metadata")
    metadata_ref = ObjectRef(
        metadata_digest, 8, "application/json", "test", "cas:metadata"
    )
    recording_ref = RecordingObjectRef(
        recording_id, data_ref, metadata_ref, metadata_digest
    )
    request = FullDwellRefinementRequestV0_1(
        SchemaRef(FullDwellRefinementRequestV0_1.SCHEMA_ID, V0_1),
        recording_id,
        recording_ref,
        ArtifactRef("fdtl_test", digest, SchemaRef("timeline", SchemaVersion(0, 1))),
        digest,
        (
            FullDwellRefinementWindowV0_1(
                RadioId("radio_test"),
                "lnb-a",
                SegmentId("seg_test"),
                ReceiverChainId("rx_0"),
                4,
                StarlinkEdge.LOWER,
                0,
                0,
                20_000,
            ),
        ),
        "top-power-then-start;pattern-blind-per-stream",
    )
    return ProducedFullDwellTimelineV0_1(
        ArtifactRef("fdtl_test", digest, SchemaRef("timeline", SchemaVersion(0, 1))),
        request,
    )


def _lease() -> FullDwellTimelineLeaseV0_1:
    result = _result()
    recording_ref = result.refinement_request.recording_object_ref
    return FullDwellTimelineLeaseV0_1(
        "work",
        "lease",
        1,
        recording_ref,
        FullDwellTimelinePlanV0_1(20_000),
        (
            FullDwellTimelineStreamSelectionV0_1(
                RadioId("radio_test"),
                "lnb-a",
                SegmentId("seg_test"),
                ReceiverChainId("rx_0"),
                4,
                StarlinkEdge.LOWER,
                2_500_000.0,
                50_000_000,
            ),
        ),
    )


class _Work:
    def __init__(self) -> None:
        self.lease = _lease()
        self.events: list[str] = []

    def claim(self, worker_id: str):
        self.events.append(f"claim:{worker_id}")
        return self.lease

    def complete_timeline(self, lease, result) -> None:
        self.events.append("complete-timeline")

    def retry_timeline(self, lease, reason: str) -> None:
        self.events.append(f"retry:{reason}")

    def record_refinement_dispatch_failure(self, lease, reason: str) -> None:
        self.events.append(f"refinement-error:{reason}")


@dataclass
class _Producer:
    fail: bool = False

    def produce(self, lease):
        if self.fail:
            raise RuntimeError("optional producer failed")
        return _result()


@dataclass
class _Dispatch:
    work: _Work
    fail: bool = False

    def dispatch(self, request) -> None:
        self.work.events.append("dispatch-refinement")
        if self.fail:
            raise RuntimeError("exact queue unavailable")


def test_timeline_failure_is_contained_inside_pull_worker() -> None:
    work = _Work()
    service = IndependentFullDwellTimelineServiceV0_1(
        work, _Producer(fail=True), _Dispatch(work), worker_id="timeline-1"
    )
    assert service.run_once()
    assert work.events == [
        "claim:timeline-1",
        "retry:timeline-production-failed",
    ]


def test_timeline_is_durable_before_optional_refinement_dispatch_failure() -> None:
    work = _Work()
    service = IndependentFullDwellTimelineServiceV0_1(
        work, _Producer(), _Dispatch(work, fail=True), worker_id="timeline-1"
    )
    assert service.run_once()
    assert work.events == [
        "claim:timeline-1",
        "complete-timeline",
        "dispatch-refinement",
        "refinement-error:refinement-dispatch-failed",
    ]


def test_service_has_no_capture_or_primary_analysis_call_path() -> None:
    # The worker is strictly pull-based: constructing it performs no action, and
    # only run_once asks its own durable optional-work repository for work.
    work = _Work()
    IndependentFullDwellTimelineServiceV0_1(
        work, _Producer(), _Dispatch(work), worker_id="timeline-1"
    )
    assert work.events == []
