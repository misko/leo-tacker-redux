"""Durable post-capture command and port contracts for Starlink candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from leo_flow.storage.ports import RecordingView

from ._validation import require_token
from .core import (
    V0_1,
    ArtifactRef,
    Digest,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
)
from .starlink import (
    RecordingStarlinkDecisionViewV0_1,
    StarlinkEdge,
    StarlinkPilotAnalysisBundleV0_1,
)
from .storage import ObjectRef, RecordingObjectRef


@dataclass(frozen=True)
class StarlinkStreamSelectionV0_1:
    """Exact recording stream and immutable templates selected for one search."""

    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    exact_template_ref: ArtifactRef
    conditioned_control_template_ref: ArtifactRef
    probe_sample_count: int

    def __post_init__(self) -> None:
        expected = SchemaRef("org.leo-flow.starlink-edge-pilot-template", V0_1)
        if (
            self.exact_template_ref.schema != expected
            or self.conditioned_control_template_ref.schema != expected
        ):
            raise ValueError("stream selection requires v0.1 pilot templates")
        if self.exact_template_ref == self.conditioned_control_template_ref:
            raise ValueError("exact and control template identities must differ")
        if (
            isinstance(self.probe_sample_count, bool)
            or not isinstance(self.probe_sample_count, int)
            or self.probe_sample_count <= 0
        ):
            raise ValueError("probe_sample_count must be a positive integer")


@dataclass(frozen=True)
class StarlinkPilotAnalysisRequestV0_1:
    """Select one exact recording and a bounded set of known-code searches."""

    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    stream_selections: tuple[StarlinkStreamSelectionV0_1, ...]
    requested_output_schema: SchemaRef

    SCHEMA_ID = "org.leo-flow.starlink-pilot-analysis-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported Starlink pilot analysis request")
        if self.recording_id != self.recording_object_ref.recording_id:
            raise ValueError("Starlink request recording identities differ")
        if self.requested_output_schema != SchemaRef(
            StarlinkPilotAnalysisBundleV0_1.SCHEMA_ID, V0_1
        ):
            raise ValueError("unsupported Starlink pilot output schema")
        if not 0 < len(self.stream_selections) <= 64:
            raise ValueError("Starlink request stream count exceeds its bound")
        keys = tuple(
            (item.segment_id, item.receiver_chain_id) for item in self.stream_selections
        )
        if len(keys) != len(set(keys)):
            raise ValueError("Starlink request stream selections must be unique")
        if tuple(sorted(keys, key=lambda item: (str(item[0]), str(item[1])))) != keys:
            raise ValueError("Starlink request stream selections must be canonical")


@dataclass(frozen=True)
class StarlinkPilotAnalysisProductRefV0_1:
    """Durable identity for one exact candidate bundle."""

    analysis_id: str
    recording_id: RecordingId
    bundle_ref: ObjectRef

    def __post_init__(self) -> None:
        require_token(self.analysis_id, "analysis_id")
        if not self.analysis_id.startswith("slanalysis_"):
            raise ValueError("Starlink analysis product identity is invalid")


class StarlinkRecordingAnalyzerV0_1(Protocol):
    def analyze_starlink(
        self,
        recording: RecordingView,
        request: StarlinkPilotAnalysisRequestV0_1,
    ) -> StarlinkPilotAnalysisBundleV0_1: ...


class RecordingStarlinkDecisionQueryPortV0_1(Protocol):
    def recording_starlink_decision(
        self, recording_id: RecordingId
    ) -> RecordingStarlinkCandidateViewV0_1: ...


@dataclass(frozen=True)
class StarlinkCandidateSummaryV0_1:
    candidate_id: str
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    search_identity_digest: Digest
    winning_epoch_sample: int
    winning_cfo_hz: float
    search_cell_count: int
    frame_support: int
    exact_score: float
    conditioned_control_score: float
    exact_minus_control_margin: float
    pss_evidence_status: str

    def __post_init__(self) -> None:
        require_token(self.candidate_id, "candidate_id")
        if self.search_cell_count <= 0 or self.frame_support <= 0:
            raise ValueError("candidate summary search bounds must be positive")
        if self.pss_evidence_status not in ("not_evaluated", "evaluated"):
            raise ValueError("candidate summary PSS state is invalid")


@dataclass(frozen=True)
class RecordingStarlinkCandidateViewV0_1:
    """Dashboard projection of candidates; never a detection verdict."""

    schema: SchemaRef
    decision: RecordingStarlinkDecisionViewV0_1
    candidates: tuple[StarlinkCandidateSummaryV0_1, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.recording-starlink-candidates"

    def __post_init__(self) -> None:
        from .starlink import StarlinkRecordingDecisionState

        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported recording Starlink candidate view")
        if self.decision.state is not StarlinkRecordingDecisionState.CANDIDATES:
            raise ValueError("candidate projection cannot carry a decision verdict")
        if self.decision.calibrated_detection_count is not None:
            raise ValueError("candidate projection cannot count detections")
        if len(self.candidates) != self.decision.search_candidate_count:
            raise ValueError("candidate projection count differs from decision summary")
        keys = tuple(item.candidate_id for item in self.candidates)
        if len(keys) != len(set(keys)):
            raise ValueError("candidate projection identities must be unique")
