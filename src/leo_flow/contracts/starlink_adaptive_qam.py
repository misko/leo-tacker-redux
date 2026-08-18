"""Additive adaptive-window QAM contract; acquired-QAM v0.3 remains immutable."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ._validation import require_token
from .core import (
    ArtifactRef,
    Digest,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    SegmentId,
    canonical_digest,
)
from .starlink import StarlinkEdge
from .starlink_acquired_constellation_pipeline import (
    StarlinkAcquiredConstellationRecordingBundleV0_3,
)
from .storage import RecordingObjectRef

V0_4 = SchemaVersion(0, 4)
MAXIMUM_ADAPTIVE_QAM_STREAMS = 16
MAXIMUM_ADAPTIVE_QAM_WINDOWS_PER_STREAM = 24


class AdaptiveQamSelectionReason(str, Enum):
    QIN_SCORE = "top-qin-score"
    QIN_MARGIN = "top-qin-minus-surrogate-margin"
    SURROGATE_SCORE = "top-surrogate-score-control"
    FILL = "bounded-score-union-fill"


@dataclass(frozen=True)
class StarlinkAdaptiveQamWindowSelectionV0_4:
    source_window_index: int
    source_start_sample: int
    source_stop_sample: int
    qam_start_sample: int
    qam_stop_sample: int
    reasons: tuple[AdaptiveQamSelectionReason, ...]
    source_qin_score: float
    source_max_surrogate_score: float
    source_qin_minus_max_surrogate: float

    def __post_init__(self) -> None:
        if (
            self.source_window_index < 0
            or self.source_start_sample < 0
            or self.source_stop_sample <= self.source_start_sample
            or self.qam_start_sample < 0
            or self.qam_stop_sample <= self.qam_start_sample
            or not self.reasons
            or self.reasons
            != tuple(sorted(set(self.reasons), key=lambda item: item.value))
        ):
            raise ValueError("adaptive QAM selection is invalid")


@dataclass(frozen=True)
class StarlinkAdaptiveQamStreamRequestV0_4:
    radio_id: RadioId
    lnb_id: str
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int
    windows: tuple[StarlinkAdaptiveQamWindowSelectionV0_4, ...]

    def __post_init__(self) -> None:
        require_token(self.lnb_id, "lnb_id")
        if (
            self.channel_number not in (1, 2, 3, 4)
            or self.sample_rate_hz <= 0
            or self.segment_sample_count <= 0
            or not self.windows
            or len(self.windows) > MAXIMUM_ADAPTIVE_QAM_WINDOWS_PER_STREAM
            or tuple(item.qam_start_sample for item in self.windows)
            != tuple(sorted(item.qam_start_sample for item in self.windows))
            or any(
                item.qam_stop_sample > self.segment_sample_count
                for item in self.windows
            )
        ):
            raise ValueError("adaptive QAM stream request is invalid")

    @property
    def identity(self) -> tuple[str, ...]:
        return tuple(
            map(
                str,
                (
                    self.radio_id,
                    self.lnb_id,
                    self.segment_id,
                    self.receiver_chain_id,
                    self.channel_number,
                    self.edge,
                ),
            )
        )


@dataclass(frozen=True)
class StarlinkAdaptiveQamRequestV0_4:
    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    source_adaptive_response_ref: ArtifactRef
    source_suite_ref: ArtifactRef
    streams: tuple[StarlinkAdaptiveQamStreamRequestV0_4, ...]
    requested_output_schema: SchemaRef

    SCHEMA_ID = "org.leo-flow.starlink-adaptive-qam-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_4):
            raise ValueError("unsupported adaptive QAM request schema")
        if self.recording_id != self.recording_object_ref.recording_id:
            raise ValueError("adaptive QAM recording identities differ")
        identities = tuple(item.identity for item in self.streams)
        if (
            not identities
            or identities != tuple(sorted(identities))
            or len(identities) != len(set(identities))
            or len(identities) > MAXIMUM_ADAPTIVE_QAM_STREAMS
            or self.requested_output_schema
            != SchemaRef(StarlinkAdaptiveQamBundleV0_4.SCHEMA_ID, V0_4)
        ):
            raise ValueError("adaptive QAM request membership is invalid")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkAdaptiveQamBundleV0_4:
    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    source_adaptive_response_ref: ArtifactRef
    source_suite_ref: ArtifactRef
    request_digest: Digest
    stream_selections: tuple[StarlinkAdaptiveQamStreamRequestV0_4, ...]
    evidence_bundle: StarlinkAcquiredConstellationRecordingBundleV0_3
    warnings: tuple[str, ...]
    calibrated_detection_count: None

    SCHEMA_ID = "org.leo-flow.starlink-adaptive-qam-bundle"

    def __post_init__(self) -> None:
        identities = tuple(item.identity for item in self.stream_selections)
        required = {
            "candidate-evidence-not-calibrated-detection",
            "adaptive-window-selection-bias-disclosed",
            "target-and-control-selected-windows-retained",
            "whole-time-epoch-cfo-search-calibration-required",
            "published-edge-pilot-not-user-payload",
        }
        if (
            self.schema != SchemaRef(self.SCHEMA_ID, V0_4)
            or not self.analysis_id.startswith("slqam4_")
            or not identities
            or identities != tuple(sorted(identities))
            or self.evidence_bundle.recording_id != self.recording_id
            or self.evidence_bundle.recording_identity_digest
            != self.recording_identity_digest
            or self.evidence_bundle.source_suite_ref != self.source_suite_ref
            or len(self.evidence_bundle.streams) != len(self.stream_selections)
            or self.calibrated_detection_count is not None
            or not required <= set(self.warnings)
        ):
            raise ValueError("adaptive QAM bundle is invalid")
        evidence_keys = tuple(
            (
                str(item.radio_id),
                str(item.segment_id),
                str(item.receiver_chain_id),
                item.edge.value,
            )
            for item in self.evidence_bundle.streams
        )
        selection_keys = tuple(
            (
                str(item.radio_id),
                str(item.segment_id),
                str(item.receiver_chain_id),
                item.edge.value,
            )
            for item in self.stream_selections
        )
        if evidence_keys != selection_keys or any(
            len(selection.windows) != len(evidence.windows)
            or any(
                selected.qam_start_sample != window.start_sample
                or selected.qam_stop_sample != window.stop_sample
                for selected, window in zip(
                    selection.windows, evidence.windows, strict=True
                )
            )
            for selection, evidence in zip(
                self.stream_selections, self.evidence_bundle.streams, strict=True
            )
        ):
            raise ValueError("adaptive QAM selection and evidence differ")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)
