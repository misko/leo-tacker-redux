"""Presentation contract for complete durable symbolwise replay curves."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._validation import require_finite, require_token
from .core import RadioId, ReceiverChainId, RecordingId, SegmentId
from .starlink import StarlinkEdge

SYMBOLWISE_REPLAY_WINDOW_COUNT = 600
SYMBOLWISE_REPLAY_WINDOW_DURATION_MS = 10
SYMBOLWISE_REPLAY_CADENCE_MS = 100
SYMBOLWISE_REPLAY_COVERAGE_FRACTION = 0.1
MAXIMUM_SYMBOLWISE_REPLAY_STREAMS = 16


@dataclass(frozen=True)
class RecordingSymbolwiseReplayDashboardQueryV0_1:
    recording_id: RecordingId
    radio_ids: tuple[RadioId, ...] = ()
    lnb_ids: tuple[str, ...] = ()
    receiver_chain_ids: tuple[ReceiverChainId, ...] = ()

    def __post_init__(self) -> None:
        for values, label in (
            (self.radio_ids, "radio_ids"),
            (self.lnb_ids, "lnb_ids"),
            (self.receiver_chain_ids, "receiver_chain_ids"),
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError(f"symbolwise dashboard {label} must be canonical")
        for value in self.lnb_ids:
            require_token(value, "lnb_id")


@dataclass(frozen=True)
class SymbolwiseReplayPatternPointV0_1:
    pattern_id: str
    pattern_role: str
    codebook_index: int | None
    candidate_label: str
    selection_score: float
    winning_cfo_hz: float
    winning_epoch_sample: int

    def __post_init__(self) -> None:
        require_token(self.pattern_id, "pattern_id")
        if not isinstance(self.candidate_label, str) or not self.candidate_label.strip():
            raise ValueError("candidate_label must be non-empty")
        if self.pattern_role not in ("qin-exact", "precommitted-surrogate"):
            raise ValueError("unsupported symbolwise pattern role")
        require_finite(self.selection_score, "selection_score")
        require_finite(self.winning_cfo_hz, "winning_cfo_hz")
        if not 0 <= self.selection_score <= 1 or self.winning_epoch_sample < 0:
            raise ValueError("invalid symbolwise response winner")


@dataclass(frozen=True)
class SymbolwiseReplayWindowPointV0_1:
    window_index: int
    start_sample: int
    stop_sample: int
    start_time_s: float
    stop_time_s: float
    patterns: tuple[SymbolwiseReplayPatternPointV0_1, ...]

    def __post_init__(self) -> None:
        if (
            self.window_index < 0
            or self.start_sample < 0
            or self.stop_sample <= self.start_sample
            or len(self.patterns) != 5
        ):
            raise ValueError("invalid symbolwise replay window point")
        require_finite(self.start_time_s, "start_time_s")
        require_finite(self.stop_time_s, "stop_time_s")
        if self.stop_time_s <= self.start_time_s:
            raise ValueError("symbolwise replay time interval must be non-empty")
        roles = tuple(item.pattern_role for item in self.patterns)
        indexes = tuple(item.codebook_index for item in self.patterns)
        if roles != ("qin-exact", *("precommitted-surrogate",) * 4) or indexes != (
            None,
            0,
            1,
            2,
            3,
        ):
            raise ValueError("symbolwise replay patterns must be Qin then surrogates")


@dataclass(frozen=True)
class SymbolwiseReplayPatternOverallV0_1:
    pattern_id: str
    pattern_role: str
    codebook_index: int | None
    candidate_label: str
    mean_selection_score: float
    maximum_selection_score: float
    winning_window_index: int
    winning_window_start_time_s: float
    winning_cfo_hz: float
    winning_epoch_sample: int
    derivation: str

    def __post_init__(self) -> None:
        require_token(self.pattern_id, "pattern_id")
        if self.pattern_role not in ("qin-exact", "precommitted-surrogate"):
            raise ValueError("unsupported symbolwise overall pattern role")
        for value, label in (
            (self.mean_selection_score, "mean_selection_score"),
            (self.maximum_selection_score, "maximum_selection_score"),
            (self.winning_window_start_time_s, "winning_window_start_time_s"),
            (self.winning_cfo_hz, "winning_cfo_hz"),
        ):
            require_finite(value, label)
        if (
            not 0 <= self.mean_selection_score <= 1
            or not 0 <= self.maximum_selection_score <= 1
            or self.mean_selection_score > self.maximum_selection_score + 1e-12
            or not 0 <= self.winning_window_index < SYMBOLWISE_REPLAY_WINDOW_COUNT
            or self.winning_epoch_sample < 0
        ):
            raise ValueError("invalid symbolwise overall winner")
        if self.derivation != (
            "arithmetic-mean-and-maximum-selection-score-over-all-600-"
            "fixed-cadence-windows;ties-first-window"
        ):
            raise ValueError("unknown symbolwise overall derivation")


@dataclass(frozen=True)
class SymbolwiseReplayDashboardStreamV0_1:
    recording_id: RecordingId
    radio_id: RadioId
    lnb_id: str
    receiver_chain_id: ReceiverChainId
    segment_id: SegmentId
    edge: StarlinkEdge
    sample_rate_hz: float
    frequency_center_cfo_hz: float
    window_count: int
    window_duration_ms: int
    cadence_ms: int
    analyzed_union_fraction: float
    analyzed_union_percent: float
    windows: tuple[SymbolwiseReplayWindowPointV0_1, ...]
    overall: tuple[SymbolwiseReplayPatternOverallV0_1, ...]
    candidates_only: bool

    def __post_init__(self) -> None:
        require_token(self.lnb_id, "lnb_id")
        require_finite(self.sample_rate_hz, "sample_rate_hz")
        require_finite(self.frequency_center_cfo_hz, "frequency_center_cfo_hz")
        if (
            self.sample_rate_hz <= 0
            or self.window_count != SYMBOLWISE_REPLAY_WINDOW_COUNT
            or len(self.windows) != SYMBOLWISE_REPLAY_WINDOW_COUNT
            or self.window_duration_ms != SYMBOLWISE_REPLAY_WINDOW_DURATION_MS
            or self.cadence_ms != SYMBOLWISE_REPLAY_CADENCE_MS
            or self.analyzed_union_fraction != SYMBOLWISE_REPLAY_COVERAGE_FRACTION
            or self.analyzed_union_percent != 10.0
            or len(self.overall) != 5
            or self.candidates_only is not True
        ):
            raise ValueError("symbolwise dashboard lost fixed full-dwell accounting")
        window_samples = round(self.sample_rate_hz * 0.010)
        cadence_samples = round(self.sample_rate_hz * 0.100)
        if any(
            point.window_index != index
            or point.start_sample != index * cadence_samples
            or point.stop_sample != index * cadence_samples + window_samples
            or point.start_time_s != point.start_sample / self.sample_rate_hz
            or point.stop_time_s != point.stop_sample / self.sample_rate_hz
            for index, point in enumerate(self.windows)
        ):
            raise ValueError("symbolwise dashboard windows are not exact fixed cadence")
        if tuple(item.pattern_id for item in self.overall) != tuple(
            item.pattern_id for item in self.windows[0].patterns
        ):
            raise ValueError("symbolwise overall summaries identify other patterns")


@dataclass(frozen=True)
class RecordingSymbolwiseReplayDashboardViewV0_1:
    recording_id: RecordingId
    streams: tuple[SymbolwiseReplayDashboardStreamV0_1, ...]
    stream_count: int
    window_count_per_stream: int
    point_count: int
    candidate_only: bool
    calibrated_detection_count: None
    summary_derivation: str
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            self.stream_count != len(self.streams)
            or not 0 <= self.stream_count <= MAXIMUM_SYMBOLWISE_REPLAY_STREAMS
            or self.window_count_per_stream != SYMBOLWISE_REPLAY_WINDOW_COUNT
            or self.point_count != self.stream_count * SYMBOLWISE_REPLAY_WINDOW_COUNT
            or self.candidate_only is not True
            or self.calibrated_detection_count is not None
        ):
            raise ValueError("invalid symbolwise dashboard view accounting")
        if self.summary_derivation != (
            "per-stream-per-pattern-only;arithmetic-mean-and-maximum-over-"
            "all-600-windows;no-cross-hardware-pooling"
        ):
            raise ValueError("invalid symbolwise dashboard summary derivation")
        if any(stream.recording_id != self.recording_id for stream in self.streams):
            raise ValueError("symbolwise dashboard stream belongs to another recording")
        if self.limitations != tuple(sorted(set(self.limitations))):
            raise ValueError("symbolwise dashboard limitations must be canonical")
        for value in self.limitations:
            require_token(value, "limitation")


class RecordingSymbolwiseReplayDashboardQueryPortV0_1(Protocol):
    def recording_symbolwise_replay_dashboard(
        self, query: RecordingSymbolwiseReplayDashboardQueryV0_1
    ) -> RecordingSymbolwiseReplayDashboardViewV0_1: ...
