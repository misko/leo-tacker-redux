"""Durable publication and bounded query contracts for pilot constellations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._validation import require_token
from .core import (
    ArtifactRef,
    Digest,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    canonical_digest,
)
from .starlink import StarlinkEdge
from .starlink_detector_suite import V0_2
from .starlink_pilot_constellation import (
    MAX_CONSTELLATION_POINTS,
    StarlinkPilotConstellationEvidenceV0_1,
    StarlinkPilotConstellationPointV0_1,
    StarlinkPilotSubcarrierSummaryV0_1,
)
from .storage import ObjectRef, RecordingObjectRef

MAX_CONSTELLATION_STREAMS = 64
MAX_CONSTELLATION_QUERY_STREAMS = 16


@dataclass(frozen=True)
class StarlinkPilotConstellationRequestV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    source_suite_ref: ArtifactRef
    source_suite_request_digest: Digest
    stream_keys: tuple[tuple[SegmentId, ReceiverChainId, StarlinkEdge], ...]
    requested_output_schema: SchemaRef

    SCHEMA_ID = "org.leo-flow.starlink-pilot-constellation-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported constellation request schema")
        if self.recording_id != self.recording_object_ref.recording_id:
            raise ValueError("request recording identities differ")
        expected_source = SchemaRef(
            "org.leo-flow.starlink-detector-suite-recording-bundle", V0_2
        )
        if self.source_suite_ref.schema != expected_source:
            raise ValueError("request must bind a detector-suite v0.2 product")
        if self.requested_output_schema != SchemaRef(
            StarlinkPilotConstellationRecordingBundleV0_1.SCHEMA_ID
        ):
            raise ValueError("unsupported constellation output schema")
        canonical = tuple(
            sorted(self.stream_keys, key=lambda value: tuple(map(str, value)))
        )
        if not self.stream_keys or self.stream_keys != canonical:
            raise ValueError("stream keys must be non-empty and canonical")
        if (
            len(set(self.stream_keys)) != len(self.stream_keys)
            or len(self.stream_keys) > MAX_CONSTELLATION_STREAMS
        ):
            raise ValueError("stream keys are duplicate or unbounded")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkPilotConstellationRecordingBundleV0_1:
    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    source_suite_ref: ArtifactRef
    source_suite_request_digest: Digest
    request_digest: Digest
    streams: tuple[StarlinkPilotConstellationEvidenceV0_1, ...]
    reason_codes: tuple[str, ...]
    calibrated_detection_count: int | None

    SCHEMA_ID = "org.leo-flow.starlink-pilot-constellation-recording-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported constellation recording schema")
        require_token(self.analysis_id, "analysis_id")
        if not self.analysis_id.startswith("slqamrec_"):
            raise ValueError("invalid constellation analysis identity")
        keys = tuple(
            (item.segment_id, item.receiver_chain_id, item.edge)
            for item in self.streams
        )
        if keys != tuple(sorted(keys, key=lambda value: tuple(map(str, value)))):
            raise ValueError("constellation streams must be canonical")
        if (
            not keys
            or len(set(keys)) != len(keys)
            or len(keys) > MAX_CONSTELLATION_STREAMS
        ):
            raise ValueError("constellation streams are empty, duplicate, or unbounded")
        if any(
            item.recording_id != self.recording_id
            or item.recording_identity_digest != self.recording_identity_digest
            for item in self.streams
        ):
            raise ValueError("constellation stream belongs to another recording")
        if self.calibrated_detection_count is not None:
            raise ValueError("constellation output cannot count detections")
        required = {
            "candidate-evidence-not-calibrated-detection",
            "published-edge-pilot-not-user-payload",
        }
        if not required <= set(self.reason_codes):
            raise ValueError("recording bundle must disclose scope")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkPilotConstellationProductRefV0_1:
    analysis_id: str
    recording_id: RecordingId
    bundle_ref: ObjectRef

    def __post_init__(self) -> None:
        require_token(self.analysis_id, "analysis_id")
        if not self.analysis_id.startswith("slqamrec_"):
            raise ValueError("invalid constellation product identity")

    @property
    def artifact_ref(self) -> ArtifactRef:
        return ArtifactRef(
            self.analysis_id,
            self.bundle_ref.digest,
            SchemaRef(StarlinkPilotConstellationRecordingBundleV0_1.SCHEMA_ID),
        )


@dataclass(frozen=True)
class StarlinkPilotConstellationCatalogProjectionV0_1:
    analysis_id: str
    recording_id: RecordingId
    input_recording_digest: Digest
    source_suite_ref: ArtifactRef
    source_suite_request_digest: Digest
    request_digest: Digest
    stream_count: int
    point_count: int

    def __post_init__(self) -> None:
        require_token(self.analysis_id, "analysis_id")
        if not 1 <= self.stream_count <= MAX_CONSTELLATION_STREAMS:
            raise ValueError("projection stream count is out of bounds")
        if self.point_count != self.stream_count * MAX_CONSTELLATION_POINTS:
            raise ValueError("projection point count is inconsistent")


@dataclass(frozen=True)
class StarlinkPilotConstellationQueryV0_1:
    recording_id: RecordingId
    segment_ids: tuple[SegmentId, ...] = ()
    receiver_chain_ids: tuple[ReceiverChainId, ...] = ()
    edges: tuple[StarlinkEdge, ...] = ()
    maximum_streams: int = MAX_CONSTELLATION_QUERY_STREAMS
    maximum_points_per_stream: int = MAX_CONSTELLATION_POINTS

    def __post_init__(self) -> None:
        for values, name in (
            (self.segment_ids, "segments"),
            (self.receiver_chain_ids, "receivers"),
            (self.edges, "edges"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"query {name} must be unique")
        if not 1 <= self.maximum_streams <= MAX_CONSTELLATION_QUERY_STREAMS:
            raise ValueError("maximum_streams is out of bounds")
        if not 1 <= self.maximum_points_per_stream <= MAX_CONSTELLATION_POINTS:
            raise ValueError("maximum_points_per_stream is out of bounds")


@dataclass(frozen=True)
class StarlinkPilotConstellationPresentationStreamV0_1:
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    evidence_analysis_id: str
    evidence_digest: Digest
    hard_symbol_accuracy: float
    rms_evm: float
    soft_mean_confidence: float
    soft_mean_entropy_bits: float
    model_snr_db: float
    residual_cfo_refinement_hz: float
    complete_frame_count: int
    subcarriers: tuple[StarlinkPilotSubcarrierSummaryV0_1, ...]
    display_points: tuple[StarlinkPilotConstellationPointV0_1, ...]
    original_point_count: int
    display_point_selection: str

    def __post_init__(self) -> None:
        require_token(self.evidence_analysis_id, "evidence_analysis_id")
        if not 1 <= len(self.display_points) <= MAX_CONSTELLATION_POINTS:
            raise ValueError("display point count is out of bounds")
        if self.original_point_count != MAX_CONSTELLATION_POINTS:
            raise ValueError("original evidence must contain the complete stack")
        if self.display_point_selection not in (
            "all-canonical-points",
            "deterministic-even-index-thinning",
        ):
            raise ValueError("unknown display point selection")


@dataclass(frozen=True)
class RecordingStarlinkPilotConstellationViewV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    analysis_ref: ArtifactRef
    source_suite_ref: ArtifactRef
    streams: tuple[StarlinkPilotConstellationPresentationStreamV0_1, ...]
    truncated: bool

    SCHEMA_ID = "org.leo-flow.dashboard.recording-starlink-pilot-constellation"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported constellation dashboard view")
        if len(self.streams) > MAX_CONSTELLATION_QUERY_STREAMS:
            raise ValueError("constellation view is unbounded")


class RecordingStarlinkPilotConstellationQueryPortV0_1(Protocol):
    def recording_starlink_pilot_constellation(
        self, query: StarlinkPilotConstellationQueryV0_1
    ) -> RecordingStarlinkPilotConstellationViewV0_1: ...


def constellation_presentation_stream(
    evidence: StarlinkPilotConstellationEvidenceV0_1, maximum: int
) -> StarlinkPilotConstellationPresentationStreamV0_1:
    """Create a bounded presentation DTO without changing durable evidence."""
    if maximum >= len(evidence.points):
        points = evidence.points
        selection = "all-canonical-points"
    else:
        indexes = tuple(
            (offset * len(evidence.points)) // maximum for offset in range(maximum)
        )
        points = tuple(evidence.points[index] for index in indexes)
        selection = "deterministic-even-index-thinning"
    return StarlinkPilotConstellationPresentationStreamV0_1(
        evidence.segment_id,
        evidence.receiver_chain_id,
        evidence.edge,
        evidence.analysis_id,
        evidence.digest,
        evidence.hard_symbol_accuracy,
        evidence.rms_evm,
        evidence.soft_mean_confidence,
        evidence.soft_mean_entropy_bits,
        evidence.model_snr_db,
        evidence.residual_cfo_refinement_hz,
        evidence.complete_frame_count,
        evidence.subcarriers,
        points,
        len(evidence.points),
        selection,
    )
