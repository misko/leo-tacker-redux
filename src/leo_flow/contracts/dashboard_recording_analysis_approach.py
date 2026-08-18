"""Additive recording-detail facts for exact analysis plans and coverage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._validation import require_finite, require_token
from .core import (
    V0_1,
    ArtifactRef,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
)
from .starlink import StarlinkEdge


@dataclass(frozen=True)
class RecordingQamAnalysisApproachStreamV0_1:
    radio_id: RadioId
    lnb_id: str
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int
    window_count: int
    window_sample_count: int
    analyzed_union_sample_count: int
    analyzed_union_fraction: float
    sampling_plan: str
    overall_derivation: str
    receiver_cfo_profile_ids: tuple[str, ...]
    searched_cfo_min_hz: float
    searched_cfo_max_hz: float
    coarse_search_cell_count: int
    refinement_search_cell_count: int
    retained_candidate_count: int
    winning_cfo_min_hz: float
    winning_cfo_max_hz: float
    hardware_calibration_state: str

    def __post_init__(self) -> None:
        require_token(self.lnb_id, "lnb_id")
        for name in (
            "sample_rate_hz",
            "analyzed_union_fraction",
            "searched_cfo_min_hz",
            "searched_cfo_max_hz",
            "winning_cfo_min_hz",
            "winning_cfo_max_hz",
        ):
            require_finite(getattr(self, name), name)
        if (
            self.sample_rate_hz <= 0
            or self.segment_sample_count <= 0
            or self.window_count <= 0
            or self.window_sample_count <= 0
            or not 0 < self.analyzed_union_sample_count <= self.segment_sample_count
            or not 0 < self.analyzed_union_fraction <= 1
        ):
            raise ValueError("recording QAM approach geometry is invalid")
        if self.searched_cfo_min_hz >= self.searched_cfo_max_hz:
            raise ValueError("recording QAM approach CFO domain is invalid")
        if self.winning_cfo_min_hz > self.winning_cfo_max_hz:
            raise ValueError("recording QAM winner CFO range is invalid")
        if (
            self.coarse_search_cell_count <= 0
            or self.refinement_search_cell_count <= 0
            or self.retained_candidate_count <= 0
        ):
            raise ValueError("recording QAM search accounting is invalid")
        if not self.receiver_cfo_profile_ids or len(
            self.receiver_cfo_profile_ids
        ) != len(set(self.receiver_cfo_profile_ids)):
            raise ValueError("recording QAM CFO profiles are empty or duplicate")
        if self.sampling_plan != "bounded-evenly-spaced-endpoint-preserving-windows":
            raise ValueError("recording QAM sampling plan is unsupported")
        if self.overall_derivation != (
            "support-weighted-window-summary;display=max-held-out-margin-window"
        ):
            raise ValueError("recording QAM overall derivation is unsupported")
        if self.hardware_calibration_state not in {
            "label-independent-wide-physical-search",
            "historical-product-profile-not-current-calibration",
        }:
            raise ValueError("recording QAM hardware-calibration state is unsupported")


@dataclass(frozen=True)
class RecordingAnalysisApproachViewV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    qam_analysis_ref: ArtifactRef
    qam_streams: tuple[RecordingQamAnalysisApproachStreamV0_1, ...]
    candidate_only: bool
    calibration_required: bool
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.recording-analysis-approach"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported recording analysis-approach schema")
        if not self.qam_streams or len(self.qam_streams) > 16:
            raise ValueError("recording QAM approach streams are empty or unbounded")
        keys = tuple(
            (item.radio_id, item.segment_id, item.receiver_chain_id, item.edge)
            for item in self.qam_streams
        )
        if keys != tuple(sorted(keys, key=lambda item: tuple(map(str, item)))):
            raise ValueError("recording QAM approach streams are noncanonical")
        if not self.candidate_only or not self.calibration_required:
            raise ValueError("recording analysis approaches must remain fail-closed")
        required = {
            "legacy-lnb-label-offsets-not-applied",
            "whole-search-null-calibration-required",
            "window-sampling-is-not-full-exact-coverage",
        }
        if not required <= set(self.warnings):
            raise ValueError("recording analysis approach warnings are incomplete")


class RecordingAnalysisApproachQueryPortV0_1(Protocol):
    def recording_analysis_approach(
        self, recording_id: RecordingId
    ) -> RecordingAnalysisApproachViewV0_1: ...
