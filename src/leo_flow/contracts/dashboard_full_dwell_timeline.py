"""Additive dashboard contract for the complete full-dwell prescreen timeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts._validation import require_finite, require_utc_ns
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.starlink import StarlinkEdge

MAXIMUM_FULL_DWELL_TIMELINE_WINDOWS = 16_384


@dataclass(frozen=True)
class FullDwellTimelineQueryV0_1:
    recording_id: RecordingId
    radio_ids: tuple[RadioId, ...] = ()
    receiver_chain_ids: tuple[ReceiverChainId, ...] = ()
    edges: tuple[StarlinkEdge, ...] = ()
    maximum_windows: int = MAXIMUM_FULL_DWELL_TIMELINE_WINDOWS

    def __post_init__(self) -> None:
        for values, label in (
            (self.radio_ids, "radios"),
            (self.receiver_chain_ids, "receivers"),
            (self.edges, "edges"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"full-dwell timeline {label} must be unique")
        if not 1 <= self.maximum_windows <= MAXIMUM_FULL_DWELL_TIMELINE_WINDOWS:
            raise ValueError("full-dwell timeline window bound is invalid")


@dataclass(frozen=True)
class FullDwellTimelineWindowV0_1:
    window_index: int
    start_sample: int
    stop_sample: int
    interval_start_utc_ns: UtcNs
    interval_stop_utc_ns: UtcNs
    mean_complex_power: float
    selected_for_exact_refinement: bool

    def __post_init__(self) -> None:
        if (
            self.window_index < 0
            or self.start_sample < 0
            or self.stop_sample <= self.start_sample
        ):
            raise ValueError("invalid full-dwell timeline interval")
        require_utc_ns(self.interval_start_utc_ns, "interval_start_utc_ns")
        require_utc_ns(self.interval_stop_utc_ns, "interval_stop_utc_ns")
        if self.interval_stop_utc_ns <= self.interval_start_utc_ns:
            raise ValueError("invalid full-dwell timeline UTC interval")
        require_finite(self.mean_complex_power, "mean_complex_power")
        if self.mean_complex_power < 0:
            raise ValueError("full-dwell timeline power cannot be negative")


@dataclass(frozen=True)
class FullDwellTimelineStreamV0_1:
    radio_id: RadioId
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int
    original_window_count: int
    prescreen_coverage_fraction: float
    exact_coverage_fraction: float
    windows: tuple[FullDwellTimelineWindowV0_1, ...]

    def __post_init__(self) -> None:
        if self.channel_number not in (1, 2, 3, 4):
            raise ValueError("timeline channel must be one of 1,2,3,4")
        require_finite(self.sample_rate_hz, "sample_rate_hz")
        if self.sample_rate_hz <= 0 or self.segment_sample_count <= 0:
            raise ValueError("timeline stream geometry is invalid")
        if self.original_window_count < len(self.windows):
            raise ValueError("timeline original window count is invalid")
        if self.prescreen_coverage_fraction != 1.0:
            raise ValueError("timeline must identify complete prescreen coverage")
        if not 0 < self.exact_coverage_fraction <= 1:
            raise ValueError("timeline exact coverage fraction is invalid")
        indices = tuple(item.window_index for item in self.windows)
        if indices != tuple(sorted(set(indices))):
            raise ValueError("timeline windows must be canonical")


@dataclass(frozen=True)
class RecordingFullDwellTimelineViewV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    analysis_ref: ArtifactRef
    prescreen_window_samples: int
    prescreen_stride_samples: int
    streams: tuple[FullDwellTimelineStreamV0_1, ...]
    original_window_count: int
    returned_window_count: int
    truncated: bool
    decimation: str
    candidate_only: bool
    calibrated_detection_count: None
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.recording-full-dwell-timeline"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported full-dwell timeline schema")
        if self.prescreen_window_samples <= 0 or self.prescreen_stride_samples <= 0:
            raise ValueError("timeline plan geometry is invalid")
        if self.original_window_count < self.returned_window_count:
            raise ValueError("timeline returned count exceeds original count")
        if self.returned_window_count != sum(
            len(stream.windows) for stream in self.streams
        ):
            raise ValueError("timeline returned count is inconsistent")
        if self.truncated != (self.returned_window_count < self.original_window_count):
            raise ValueError("timeline truncation state is inconsistent")
        if not self.candidate_only or self.calibrated_detection_count is not None:
            raise ValueError("timeline cannot claim calibrated detection")
        required = {
            "prescreen-window-union-covers-full-dwell",
            "power-prescreen-is-not-starlink-detection",
            "exact-detector-windows-are-selected-not-full-coverage",
        }
        if not required <= set(self.warnings):
            raise ValueError("timeline disclosures are incomplete")


class RecordingFullDwellTimelineQueryPortV0_1(Protocol):
    def recording_full_dwell_timeline(
        self, query: FullDwellTimelineQueryV0_1
    ) -> RecordingFullDwellTimelineViewV0_1: ...
