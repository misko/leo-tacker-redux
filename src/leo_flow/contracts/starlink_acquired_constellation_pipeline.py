"""Durable and bounded presentation contracts for acquired QAM v0.3."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ._validation import require_finite, require_token, require_utc_ns
from .core import (
    ArtifactRef,
    Digest,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
    canonical_digest,
)
from .starlink import StarlinkEdge
from .starlink_acquired_constellation import (
    StarlinkAcquiredPilotConstellationEvidenceV0_3,
)
from .starlink_acquisition import V0_3, StarlinkAcquisitionBundleV0_3
from .starlink_pilot_constellation import (
    MAX_CONSTELLATION_POINTS,
    StarlinkPilotConstellationPointV0_1,
    StarlinkPilotSubcarrierSummaryV0_1,
)
from .storage import ObjectRef, RecordingObjectRef

MAX_ACQUIRED_QAM_STREAMS = 16
MAX_ACQUIRED_QAM_WINDOWS_PER_STREAM = 32
MAX_ACQUIRED_QAM_QUERY_STREAMS = 16
MAX_ACQUIRED_QAM_QUERY_WINDOWS = 32


class StarlinkAcquiredConstellationViewMode(str, Enum):
    OVERALL = "overall"
    WINDOWS = "windows"


@dataclass(frozen=True)
class StarlinkAcquiredConstellationRequestV0_3:
    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    source_suite_ref: ArtifactRef
    source_suite_request_digest: Digest
    stream_keys: tuple[tuple[RadioId, SegmentId, ReceiverChainId, StarlinkEdge], ...]
    maximum_windows_per_stream: int
    requested_output_schema: SchemaRef

    SCHEMA_ID = "org.leo-flow.starlink-acquired-constellation-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_3):
            raise ValueError("unsupported acquired-QAM request schema")
        if self.recording_id != self.recording_object_ref.recording_id:
            raise ValueError("acquired-QAM request recording identities differ")
        if self.requested_output_schema != SchemaRef(
            StarlinkAcquiredConstellationRecordingBundleV0_3.SCHEMA_ID, V0_3
        ):
            raise ValueError("unsupported acquired-QAM output schema")
        canonical = tuple(
            sorted(self.stream_keys, key=lambda value: tuple(map(str, value)))
        )
        if not self.stream_keys or self.stream_keys != canonical:
            raise ValueError("acquired-QAM stream keys must be non-empty and canonical")
        if (
            len(set(self.stream_keys)) != len(self.stream_keys)
            or len(self.stream_keys) > MAX_ACQUIRED_QAM_STREAMS
        ):
            raise ValueError("acquired-QAM stream keys are duplicate or unbounded")
        if (
            not 1
            <= self.maximum_windows_per_stream
            <= MAX_ACQUIRED_QAM_WINDOWS_PER_STREAM
        ):
            raise ValueError("acquired-QAM window bound is invalid")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkAcquiredConstellationWindowV0_3:
    window_index: int
    start_sample: int
    stop_sample: int
    interval_start_utc_ns: UtcNs
    interval_stop_utc_ns: UtcNs
    acquisition: StarlinkAcquisitionBundleV0_3
    evidence: StarlinkAcquiredPilotConstellationEvidenceV0_3

    def __post_init__(self) -> None:
        if (
            self.window_index < 0
            or self.start_sample < 0
            or self.stop_sample <= self.start_sample
        ):
            raise ValueError("invalid acquired-QAM window")
        require_utc_ns(self.interval_start_utc_ns, "interval_start_utc_ns")
        require_utc_ns(self.interval_stop_utc_ns, "interval_stop_utc_ns")
        if self.interval_stop_utc_ns <= self.interval_start_utc_ns:
            raise ValueError("invalid acquired-QAM UTC window")
        if (
            self.stop_sample - self.start_sample != self.acquisition.probe_sample_count
            or self.acquisition.probe_sample_count != self.evidence.probe_sample_count
        ):
            raise ValueError("acquired-QAM window dimensions differ")
        if self.acquisition.ref != self.evidence.source_acquisition_ref:
            raise ValueError("acquired-QAM window source closure differs")


@dataclass(frozen=True)
class StarlinkAcquiredConstellationOverallV0_3:
    window_count: int
    complete_frame_count: int
    support_weighted_hard_symbol_accuracy: float
    support_weighted_rms_evm: float
    support_weighted_model_snr_db: float
    maximum_held_out_verify_score: float
    maximum_verify_minus_control_margin: float
    selected_display_window_index: int
    derivation: str = (
        "support-weighted-window-summary;display=max-held-out-margin-window"
    )

    def __post_init__(self) -> None:
        for name in (
            "support_weighted_hard_symbol_accuracy",
            "support_weighted_rms_evm",
            "support_weighted_model_snr_db",
            "maximum_held_out_verify_score",
            "maximum_verify_minus_control_margin",
        ):
            require_finite(getattr(self, name), name)
        if (
            self.window_count <= 0
            or self.complete_frame_count <= 0
            or not 0 <= self.selected_display_window_index < self.window_count
        ):
            raise ValueError("invalid acquired-QAM overall support")
        if (
            not 0 <= self.support_weighted_hard_symbol_accuracy <= 1
            or self.support_weighted_rms_evm < 0
        ):
            raise ValueError("invalid acquired-QAM overall metrics")
        if (
            self.derivation
            != "support-weighted-window-summary;display=max-held-out-margin-window"
        ):
            raise ValueError("unknown acquired-QAM overall derivation")


@dataclass(frozen=True)
class StarlinkAcquiredConstellationStreamV0_3:
    radio_id: RadioId
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int
    windows: tuple[StarlinkAcquiredConstellationWindowV0_3, ...]
    overall: StarlinkAcquiredConstellationOverallV0_3

    def __post_init__(self) -> None:
        require_finite(self.sample_rate_hz, "sample_rate_hz")
        if self.sample_rate_hz <= 0 or self.segment_sample_count <= 0:
            raise ValueError("invalid acquired-QAM stream dimensions")
        if not self.windows or len(self.windows) > MAX_ACQUIRED_QAM_WINDOWS_PER_STREAM:
            raise ValueError("acquired-QAM windows are empty or unbounded")
        if tuple(item.window_index for item in self.windows) != tuple(
            range(len(self.windows))
        ):
            raise ValueError("acquired-QAM windows must be contiguous")
        if any(item.stop_sample > self.segment_sample_count for item in self.windows):
            raise ValueError("acquired-QAM window exceeds segment")
        if self.overall.window_count != len(self.windows):
            raise ValueError("acquired-QAM overall/window membership differs")
        for window in self.windows:
            key = window.acquisition
            if (
                key.segment_id,
                key.receiver_chain_id,
                key.edge,
                key.sample_rate_hz,
            ) != (
                self.segment_id,
                self.receiver_chain_id,
                self.edge,
                self.sample_rate_hz,
            ):
                raise ValueError("acquired-QAM window belongs to another stream")


@dataclass(frozen=True)
class StarlinkAcquiredConstellationRecordingBundleV0_3:
    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    source_suite_ref: ArtifactRef
    source_suite_request_digest: Digest
    request_digest: Digest
    streams: tuple[StarlinkAcquiredConstellationStreamV0_3, ...]
    reason_codes: tuple[str, ...]
    calibrated_detection_count: int | None

    SCHEMA_ID = "org.leo-flow.starlink-acquired-constellation-recording-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_3):
            raise ValueError("unsupported acquired-QAM recording schema")
        require_token(self.analysis_id, "analysis_id")
        if not self.analysis_id.startswith("slqam3rec_"):
            raise ValueError("invalid acquired-QAM recording identity")
        keys = tuple(
            (item.radio_id, item.segment_id, item.receiver_chain_id, item.edge)
            for item in self.streams
        )
        if (
            not keys
            or keys != tuple(sorted(keys, key=lambda value: tuple(map(str, value))))
            or len(set(keys)) != len(keys)
            or len(keys) > MAX_ACQUIRED_QAM_STREAMS
        ):
            raise ValueError("acquired-QAM streams are noncanonical or unbounded")
        for stream in self.streams:
            for window in stream.windows:
                if (
                    window.acquisition.recording_id != self.recording_id
                    or window.evidence.recording_id != self.recording_id
                    or window.acquisition.recording_identity_digest
                    != self.recording_identity_digest
                    or window.evidence.recording_identity_digest
                    != self.recording_identity_digest
                ):
                    raise ValueError("acquired-QAM recording source closure differs")
        if self.calibrated_detection_count is not None:
            raise ValueError("uncalibrated acquired-QAM cannot count detections")
        required = {
            "candidate-evidence-not-calibrated-detection",
            "whole-revised-search-calibration-required",
            "published-edge-pilot-not-user-payload",
            "bounded-window-sampling-across-dwell",
        }
        if not required <= set(self.reason_codes):
            raise ValueError("acquired-QAM recording bundle omits limitations")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkAcquiredConstellationProductRefV0_3:
    analysis_id: str
    recording_id: RecordingId
    bundle_ref: ObjectRef

    @property
    def artifact_ref(self) -> ArtifactRef:
        return ArtifactRef(
            self.analysis_id,
            self.bundle_ref.digest,
            SchemaRef(StarlinkAcquiredConstellationRecordingBundleV0_3.SCHEMA_ID, V0_3),
        )


@dataclass(frozen=True)
class StarlinkAcquiredConstellationCatalogProjectionV0_3:
    analysis_id: str
    recording_id: RecordingId
    input_recording_digest: Digest
    source_suite_ref: ArtifactRef
    source_suite_request_digest: Digest
    request_digest: Digest
    stream_count: int
    window_count: int
    point_count: int
    calibration_required: bool


@dataclass(frozen=True)
class StarlinkAcquiredConstellationQueryV0_3:
    recording_id: RecordingId
    mode: StarlinkAcquiredConstellationViewMode = (
        StarlinkAcquiredConstellationViewMode.OVERALL
    )
    radio_ids: tuple[RadioId, ...] = ()
    lnb_ids: tuple[str, ...] = ()
    segment_ids: tuple[SegmentId, ...] = ()
    receiver_chain_ids: tuple[ReceiverChainId, ...] = ()
    edges: tuple[StarlinkEdge, ...] = ()
    maximum_streams: int = MAX_ACQUIRED_QAM_QUERY_STREAMS
    maximum_windows_per_stream: int = MAX_ACQUIRED_QAM_QUERY_WINDOWS
    maximum_points_per_constellation: int = MAX_CONSTELLATION_POINTS

    def __post_init__(self) -> None:
        for values, name in (
            (self.radio_ids, "radios"),
            (self.lnb_ids, "LNBs"),
            (self.segment_ids, "segments"),
            (self.receiver_chain_ids, "receivers"),
            (self.edges, "edges"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"query {name} must be unique")
        if any(not value for value in self.lnb_ids):
            raise ValueError("query LNB IDs cannot be empty")
        if (
            not 1 <= self.maximum_streams <= MAX_ACQUIRED_QAM_QUERY_STREAMS
            or not 1
            <= self.maximum_windows_per_stream
            <= MAX_ACQUIRED_QAM_QUERY_WINDOWS
            or not 1
            <= self.maximum_points_per_constellation
            <= MAX_CONSTELLATION_POINTS
        ):
            raise ValueError("acquired-QAM query bound is invalid")


@dataclass(frozen=True)
class StarlinkAcquiredConstellationPresentationWindowV0_3:
    window_index: int
    start_sample: int
    stop_sample: int
    interval_start_utc_ns: UtcNs
    interval_stop_utc_ns: UtcNs
    evidence_analysis_id: str
    winning_epoch_sample: int
    winning_cfo_hz: float
    held_out_verify_score: float
    conditioned_control_score: float
    verify_minus_control_margin: float
    hard_symbol_accuracy: float
    rms_evm: float
    model_snr_db: float
    residual_cfo_refinement_hz: float
    complete_frame_count: int
    subcarriers: tuple[StarlinkPilotSubcarrierSummaryV0_1, ...]
    display_points: tuple[StarlinkPilotConstellationPointV0_1, ...]
    original_point_count: int
    display_point_selection: str


@dataclass(frozen=True)
class StarlinkAcquiredConstellationPresentationStreamV0_3:
    radio_id: RadioId
    lnb_id: str
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int
    overall: StarlinkAcquiredConstellationOverallV0_3
    windows: tuple[StarlinkAcquiredConstellationPresentationWindowV0_3, ...]
    original_window_count: int


@dataclass(frozen=True)
class RecordingStarlinkAcquiredConstellationViewV0_3:
    schema: SchemaRef
    recording_id: RecordingId
    analysis_ref: ArtifactRef
    source_suite_ref: ArtifactRef
    mode: StarlinkAcquiredConstellationViewMode
    streams: tuple[StarlinkAcquiredConstellationPresentationStreamV0_3, ...]
    truncated: bool
    candidate_only: bool
    calibration_required: bool

    SCHEMA_ID = "org.leo-flow.dashboard.recording-starlink-acquired-constellation"

    def __post_init__(self) -> None:
        if (
            self.schema != SchemaRef(self.SCHEMA_ID, V0_3)
            or len(self.streams) > MAX_ACQUIRED_QAM_QUERY_STREAMS
        ):
            raise ValueError("unsupported or unbounded acquired-QAM dashboard view")
        if not self.candidate_only or not self.calibration_required:
            raise ValueError("acquired-QAM dashboard must remain fail-closed")


class RecordingStarlinkAcquiredConstellationQueryPortV0_3(Protocol):
    def recording_starlink_acquired_constellation(
        self, query: StarlinkAcquiredConstellationQueryV0_3
    ) -> RecordingStarlinkAcquiredConstellationViewV0_3: ...


class RecordingReceiverLnbResolverV0_3(Protocol):
    def lnb_id_for_recording_receiver(
        self, recording_id: RecordingId, receiver_chain_id: ReceiverChainId
    ) -> str: ...


def acquired_constellation_presentation_window(
    window: StarlinkAcquiredConstellationWindowV0_3, maximum: int
) -> StarlinkAcquiredConstellationPresentationWindowV0_3:
    evidence = window.evidence
    if maximum >= len(evidence.points):
        points, selection = evidence.points, "all-canonical-points"
    else:
        indexes = tuple(
            (offset * len(evidence.points)) // maximum for offset in range(maximum)
        )
        points, selection = (
            tuple(evidence.points[index] for index in indexes),
            "deterministic-even-index-thinning",
        )
    return StarlinkAcquiredConstellationPresentationWindowV0_3(
        window.window_index,
        window.start_sample,
        window.stop_sample,
        window.interval_start_utc_ns,
        window.interval_stop_utc_ns,
        evidence.analysis_id,
        evidence.winning_epoch_sample,
        evidence.winning_cfo_hz,
        evidence.held_out_verify_score,
        evidence.conditioned_control_score,
        evidence.verify_minus_control_margin,
        evidence.hard_symbol_accuracy,
        evidence.rms_evm,
        evidence.model_snr_db,
        evidence.residual_cfo_refinement_hz,
        evidence.complete_frame_count,
        evidence.subcarriers,
        points,
        len(evidence.points),
        selection,
    )
