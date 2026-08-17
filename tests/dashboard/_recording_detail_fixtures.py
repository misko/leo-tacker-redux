from __future__ import annotations

from leo_flow.contracts.capture import ActivityKind, GainMode
from leo_flow.contracts.core import (
    V0_1,
    ActivityId,
    AnalysisRunId,
    ArtifactRef,
    Digest,
    HardwareSnapshotId,
    PlanId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    StationId,
    UtcNs,
)
from leo_flow.contracts.dashboard_recording import (
    RecordingCaptureDetailViewV0_1,
    RecordingSegmentViewV0_1,
)
from leo_flow.contracts.dashboard_waterfall import (
    RecordingWaterfallViewV0_1,
    WaterfallProjectionState,
    WaterfallTileViewV0_1,
)
from leo_flow.contracts.starlink import (
    RecordingStarlinkDecisionViewV0_1,
    StarlinkEdge,
    StarlinkRecordingDecisionState,
)
from leo_flow.contracts.starlink_pipeline import (
    RecordingStarlinkCandidateViewV0_1,
    StarlinkCandidateSummaryV0_1,
)
from leo_flow.dashboard import DashboardNotFound
from leo_flow.dashboard.repository import (
    InMemoryDashboardRepository,
    RecordingCaptureDetailProjection,
    RecordingWaterfallProjection,
)

RECORDING_ID = RecordingId("rec_1")
RECORDING_IDENTITY_DIGEST = Digest.sha256(b"rec_1-immutable-identity")


def capture_detail() -> RecordingCaptureDetailViewV0_1:
    segment = RecordingSegmentViewV0_1(
        SegmentId("seg_dashboard"),
        ActivityId("act_dashboard"),
        ActivityKind.SCAN,
        (ReceiverChainId("rx_a"), ReceiverChainId("rx_b")),
        UtcNs(100),
        UtcNs(110),
        10_755_000_000.0,
        5_000_000.0,
        5_000_000.0,
        GainMode.MANUAL,
        42.0,
        50_000,
    )
    return RecordingCaptureDetailViewV0_1(
        SchemaRef(RecordingCaptureDetailViewV0_1.SCHEMA_ID, V0_1),
        RECORDING_ID,
        PlanId("plan_dashboard"),
        StationId("station_gauss"),
        RadioId("radio_a"),
        "serial-19f2",
        HardwareSnapshotId("hw_dashboard"),
        "leo-v5-capture",
        "host-disciplined",
        UtcNs(100),
        UtcNs(120),
        "complete",
        True,
        RECORDING_IDENTITY_DIGEST,
        "<i2",
        ("sample", "receiver", "component"),
        (segment,),
    )


def waterfall() -> RecordingWaterfallViewV0_1:
    tile = WaterfallTileViewV0_1(
        SegmentId("seg_dashboard"),
        ReceiverChainId("rx_a"),
        UtcNs(100),
        50_000,
        10_755_000_000.0,
        5_000_000.0,
        256,
        (0, 1_000),
        (256, 1_256),
        (UtcNs(25_700), UtcNs(225_700)),
        (-2_500_000.0, 0.0, 2_480_468.75),
        ((-82.0, -63.0, -78.0), (-80.0, -48.0, -74.0)),
        "counts-squared-per-bin",
        -90.0,
        -40.0,
    )
    return RecordingWaterfallViewV0_1(
        SchemaRef(RecordingWaterfallViewV0_1.SCHEMA_ID, V0_1),
        RECORDING_ID,
        RECORDING_IDENTITY_DIGEST,
        AnalysisRunId("arun_dashboard_waterfall"),
        WaterfallProjectionState.COMPLETE,
        None,
        (tile,),
    )


def detail_repository() -> InMemoryDashboardRepository:
    return InMemoryDashboardRepository(
        recording_capture_details=(
            RecordingCaptureDetailProjection(capture_detail(), 1),
        ),
        recording_waterfalls=(RecordingWaterfallProjection(waterfall(), 1),),
    )


def starlink_candidates() -> RecordingStarlinkCandidateViewV0_1:
    analysis_ref = ArtifactRef(
        "slanalysis_dashboard",
        Digest.sha256(b"starlink-dashboard-bundle"),
        SchemaRef("org.leo-flow.starlink-pilot-analysis-bundle", V0_1),
    )
    decision = RecordingStarlinkDecisionViewV0_1(
        SchemaRef(RecordingStarlinkDecisionViewV0_1.SCHEMA_ID, V0_1),
        RECORDING_ID,
        StarlinkRecordingDecisionState.CANDIDATES,
        1,
        1,
        None,
        analysis_ref,
        ("whole-search-calibration-required",),
    )
    candidate = StarlinkCandidateSummaryV0_1(
        "slcandidate_dashboard",
        SegmentId("seg_dashboard"),
        ReceiverChainId("rx_a"),
        StarlinkEdge.LOWER,
        Digest.sha256(b"search-identity"),
        17,
        -1250.0,
        4096,
        9,
        0.82,
        0.41,
        0.41,
        "not_evaluated",
    )
    return RecordingStarlinkCandidateViewV0_1(
        SchemaRef(RecordingStarlinkCandidateViewV0_1.SCHEMA_ID, V0_1),
        decision,
        (candidate,),
    )


class RecordingDetailFixtureQueries:
    def __init__(
        self,
        base: object,
        starlink: RecordingStarlinkCandidateViewV0_1 | None = None,
    ) -> None:
        self._base = base
        self._detail = detail_repository()
        self._starlink = starlink

    def __getattr__(self, name: str):
        return getattr(self._base, name)

    def recording_capture_detail(self, recording_id: RecordingId):
        return self._detail.recording_capture_detail(recording_id)

    def recording_waterfall(self, recording_id: RecordingId):
        return self._detail.recording_waterfall(recording_id)

    def recording_starlink_decision(self, recording_id: RecordingId):
        if (
            self._starlink is not None
            and self._starlink.decision.recording_id == recording_id
        ):
            return self._starlink
        raise DashboardNotFound(
            f"Starlink candidates for recording {recording_id} were not found"
        )
