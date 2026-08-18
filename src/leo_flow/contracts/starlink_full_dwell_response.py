"""Additive contracts for bounded, full-dwell Starlink detector responses.

This product is deliberately separate from the v0.1 temporal-pilot product.
It makes exact interval coverage, adaptive refinement, and dependence explicit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ._validation import require_finite, require_token, require_utc_ns
from .core import (
    ArtifactRef,
    Digest,
    Provenance,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    SegmentId,
    UtcNs,
    canonical_digest,
)
from .starlink import StarlinkEdge
from .starlink_detector_suite import REPORT_METHOD_ORDER, StarlinkDetectorMethod
from .starlink_surrogate_null import (
    MAXIMUM_SURROGATES,
    StarlinkPatternSearchMode,
    StarlinkSearchGridV0_1,
)
from .storage import ObjectRef, RecordingObjectRef

V0_1 = SchemaVersion(0, 1)
MAXIMUM_FULL_DWELL_STREAMS = 16
MAXIMUM_EXACT_WINDOWS_PER_STREAM = 512
MAXIMUM_RESPONSE_SCORE_RECORDS = 262_144
MAXIMUM_FULL_DWELL_QUERY_POINTS = 4096


class StarlinkWindowTier(str, Enum):
    EXACT_REFINEMENT = "exact-refinement"


@dataclass(frozen=True)
class StarlinkFullDwellPlanV0_1:
    """Frozen two-tier plan: exhaustive cheap cover, then selected exact work."""

    coarse_window_sample_count: int
    coarse_stride_samples: int
    fine_window_sample_count: int
    maximum_prescreen_window_count: int
    maximum_fine_window_count: int
    surrogate_count: int
    prescreen_metric: str = "mean-complex-power"
    refinement_selection_rule: str = "top-power-then-start;pattern-blind-per-stream"

    def __post_init__(self) -> None:
        for name in (
            "coarse_window_sample_count",
            "coarse_stride_samples",
            "fine_window_sample_count",
            "maximum_prescreen_window_count",
            "maximum_fine_window_count",
            "surrogate_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.coarse_stride_samples > self.coarse_window_sample_count:
            raise ValueError("coarse prescreen would leave gaps in dwell coverage")
        if self.fine_window_sample_count > self.coarse_window_sample_count:
            raise ValueError("fine windows cannot exceed coarse windows")
        if self.maximum_fine_window_count >= MAXIMUM_EXACT_WINDOWS_PER_STREAM:
            raise ValueError("fine-window count leaves no room for coarse coverage")
        if self.maximum_prescreen_window_count > 16_384:
            raise ValueError("prescreen window count exceeds its resource bound")
        if self.surrogate_count > MAXIMUM_SURROGATES:
            raise ValueError("surrogate count exceeds the precommitted codebook")
        if self.prescreen_metric != "mean-complex-power":
            raise ValueError("unsupported full-dwell prescreen metric")
        if (
            self.refinement_selection_rule
            != "top-power-then-start;pattern-blind-per-stream"
        ):
            raise ValueError("unsupported refinement selection rule")


@dataclass(frozen=True)
class StarlinkFullDwellStreamSelectionV0_1:
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int

    def __post_init__(self) -> None:
        require_finite(self.sample_rate_hz, "sample_rate_hz")
        if self.sample_rate_hz <= 0 or self.segment_sample_count <= 0:
            raise ValueError("full-dwell stream dimensions must be positive")


@dataclass(frozen=True)
class StarlinkFullDwellRequestV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    source_suite_ref: ArtifactRef
    source_suite_request_digest: Digest
    search_grid: StarlinkSearchGridV0_1
    plan: StarlinkFullDwellPlanV0_1
    stream_selections: tuple[StarlinkFullDwellStreamSelectionV0_1, ...]
    requested_output_schema: SchemaRef

    SCHEMA_ID = "org.leo-flow.starlink-full-dwell-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported full-dwell request schema")
        if self.requested_output_schema != SchemaRef(
            StarlinkFullDwellResponseBundleV0_1.SCHEMA_ID, V0_1
        ):
            raise ValueError("unsupported full-dwell output schema")
        if self.recording_id != self.recording_object_ref.recording_id:
            raise ValueError("full-dwell recording identities differ")
        keys = tuple(
            (item.segment_id, item.receiver_chain_id, item.edge)
            for item in self.stream_selections
        )
        if not keys or keys != tuple(
            sorted(keys, key=lambda value: tuple(map(str, value)))
        ):
            raise ValueError(
                "full-dwell stream selections must be nonempty and canonical"
            )
        if len(keys) != len(set(keys)) or len(keys) > MAXIMUM_FULL_DWELL_STREAMS:
            raise ValueError("full-dwell streams are duplicate or unbounded")
        score_records = (
            len(keys)
            * self.plan.maximum_fine_window_count
            * len(REPORT_METHOD_ORDER)
            * (1 + self.plan.surrogate_count)
        )
        if score_records > MAXIMUM_RESPONSE_SCORE_RECORDS:
            raise ValueError("full-dwell response score budget exceeds its bound")
        if any(
            item.segment_sample_count < self.plan.coarse_window_sample_count
            for item in self.stream_selections
        ):
            raise ValueError("coarse full-dwell window exceeds selected segment")
        if self.plan.fine_window_sample_count > self.search_grid.maximum_probe_samples:
            raise ValueError("exact refinement exceeds detector probe bound")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkFullDwellWinnerV0_1:
    score: float
    winning_epoch_sample_in_window: int
    winning_epoch_sample_in_segment: int
    winning_coarse_cfo_hz: float
    winning_residual_cfo_hz: float
    effective_search_cell_count: int
    search_mode: StarlinkPatternSearchMode
    aggregation: str = "maximum-over-declared-epoch-cfo-cells"

    def __post_init__(self) -> None:
        for name in ("score", "winning_coarse_cfo_hz", "winning_residual_cfo_hz"):
            require_finite(getattr(self, name), name)
        if not 0 <= self.score <= 1:
            raise ValueError("window response score must lie in [0,1]")
        if self.winning_epoch_sample_in_window < 0:
            raise ValueError("winning window epoch must be non-negative")
        if self.winning_epoch_sample_in_segment < self.winning_epoch_sample_in_window:
            raise ValueError("winning segment epoch is inconsistent")
        if self.effective_search_cell_count <= 0:
            raise ValueError("window response requires searched cells")
        if self.aggregation != "maximum-over-declared-epoch-cfo-cells":
            raise ValueError("window response must store a search maximum")


@dataclass(frozen=True)
class StarlinkFullDwellSurrogateV0_1:
    codebook_index: int
    template_digest: Digest
    winner: StarlinkFullDwellWinnerV0_1

    def __post_init__(self) -> None:
        if not 0 <= self.codebook_index < MAXIMUM_SURROGATES:
            raise ValueError("surrogate codebook index is out of bounds")


@dataclass(frozen=True)
class StarlinkFullDwellPrescreenWindowV0_1:
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
            raise ValueError("invalid full-dwell prescreen interval")
        require_utc_ns(self.interval_start_utc_ns, "interval_start_utc_ns")
        require_utc_ns(self.interval_stop_utc_ns, "interval_stop_utc_ns")
        if self.interval_stop_utc_ns <= self.interval_start_utc_ns:
            raise ValueError("invalid full-dwell prescreen UTC interval")
        require_finite(self.mean_complex_power, "mean_complex_power")
        if self.mean_complex_power < 0:
            raise ValueError("prescreen power cannot be negative")


@dataclass(frozen=True)
class StarlinkFullDwellPointV0_1:
    recording_id: RecordingId
    radio_id: RadioId
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    method: StarlinkDetectorMethod
    window_index: int
    tier: StarlinkWindowTier
    start_sample: int
    stop_sample: int
    interval_start_utc_ns: UtcNs
    interval_stop_utc_ns: UtcNs
    prescreen_score: float
    qin: StarlinkFullDwellWinnerV0_1
    surrogates: tuple[StarlinkFullDwellSurrogateV0_1, ...]
    finite_upper_tail_rank: int
    qin_minus_max_surrogate: float
    dependence_group: str

    def __post_init__(self) -> None:
        if (
            self.window_index < 0
            or self.start_sample < 0
            or self.stop_sample <= self.start_sample
        ):
            raise ValueError("invalid full-dwell window interval")
        require_utc_ns(self.interval_start_utc_ns, "interval_start_utc_ns")
        require_utc_ns(self.interval_stop_utc_ns, "interval_stop_utc_ns")
        if self.interval_stop_utc_ns <= self.interval_start_utc_ns:
            raise ValueError("invalid full-dwell UTC interval")
        require_finite(self.prescreen_score, "prescreen_score")
        if self.prescreen_score < 0:
            raise ValueError("power prescreen score cannot be negative")
        require_token(self.dependence_group, "dependence_group")
        indices = tuple(item.codebook_index for item in self.surrogates)
        if not indices or indices != tuple(range(len(indices))):
            raise ValueError("full-dwell surrogates must be complete and canonical")
        if any(
            item.winner.winning_epoch_sample_in_segment
            != self.start_sample + item.winner.winning_epoch_sample_in_window
            for item in self.surrogates
        ) or self.qin.winning_epoch_sample_in_segment != (
            self.start_sample + self.qin.winning_epoch_sample_in_window
        ):
            raise ValueError("window and segment winner coordinates differ")
        if any(
            winner.winning_epoch_sample_in_window
            >= self.stop_sample - self.start_sample
            for winner in (self.qin, *(item.winner for item in self.surrogates))
        ):
            raise ValueError("winning epoch lies outside its exact window")
        expected_rank = 1 + sum(
            item.winner.score >= self.qin.score for item in self.surrogates
        )
        if self.finite_upper_tail_rank != expected_rank:
            raise ValueError("full-dwell finite rank is inconsistent")
        expected_margin = self.qin.score - max(
            item.winner.score for item in self.surrogates
        )
        if not math.isclose(
            self.qin_minus_max_surrogate, expected_margin, abs_tol=1e-15
        ):
            raise ValueError("full-dwell paired margin is inconsistent")
        geometry = (
            self.qin.effective_search_cell_count,
            self.qin.search_mode,
            self.qin.aggregation,
        )
        if any(
            (
                item.winner.effective_search_cell_count,
                item.winner.search_mode,
                item.winner.aggregation,
            )
            != geometry
            for item in self.surrogates
        ):
            raise ValueError("Qin and surrogates must share identical search geometry")


@dataclass(frozen=True)
class StarlinkFullDwellStreamResponseV0_1:
    radio_id: RadioId
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int
    prescreen_windows: tuple[StarlinkFullDwellPrescreenWindowV0_1, ...]
    exact_window_starts: tuple[int, ...]
    points: tuple[StarlinkFullDwellPointV0_1, ...]
    prescreen_covered_sample_count: int
    prescreen_coverage_fraction: float
    exact_covered_sample_count: int
    exact_coverage_fraction: float
    prescreen_overlap_fraction: float
    refinement_is_data_adaptive: bool

    def __post_init__(self) -> None:
        if self.channel_number not in (1, 2, 3, 4):
            raise ValueError("full-dwell channel must be one of 1,2,3,4")
        prescreen_starts = tuple(item.start_sample for item in self.prescreen_windows)
        if prescreen_starts != tuple(sorted(set(prescreen_starts))):
            raise ValueError("prescreen windows must be canonical")
        if tuple(item.window_index for item in self.prescreen_windows) != tuple(
            range(len(self.prescreen_windows))
        ):
            raise ValueError("prescreen window indices must be canonical")
        if self.exact_window_starts != tuple(sorted(set(self.exact_window_starts))):
            raise ValueError("exact starts must be canonical")
        selected = tuple(
            item.start_sample
            for item in self.prescreen_windows
            if item.selected_for_exact_refinement
        )
        if selected != self.exact_window_starts:
            raise ValueError("prescreen selections and exact starts differ")
        windows = tuple(
            (StarlinkWindowTier.EXACT_REFINEMENT, index, method)
            for index in range(len(self.exact_window_starts))
            for method in REPORT_METHOD_ORDER
        )
        actual = tuple(
            (item.tier, item.window_index, item.method) for item in self.points
        )
        if actual != windows:
            raise ValueError("every exact window must contain every detector method")
        if len(self.exact_window_starts) > MAXIMUM_EXACT_WINDOWS_PER_STREAM:
            raise ValueError("full-dwell exact windows exceed their bound")
        if (
            self.prescreen_covered_sample_count != self.segment_sample_count
            or self.prescreen_coverage_fraction != 1.0
        ):
            raise ValueError("cheap prescreen must cover the full dwell")
        prescreen_union = _union_sample_count(
            tuple(
                (item.start_sample, item.stop_sample) for item in self.prescreen_windows
            )
        )
        if prescreen_union != self.prescreen_covered_sample_count:
            raise ValueError("prescreen covered sample count is inconsistent")
        if not 0 < self.exact_covered_sample_count <= self.segment_sample_count:
            raise ValueError("exact covered sample count is invalid")
        if not math.isclose(
            self.exact_coverage_fraction,
            self.exact_covered_sample_count / self.segment_sample_count,
            abs_tol=1e-15,
        ):
            raise ValueError("exact detector coverage fraction is inconsistent")
        exact_intervals = tuple(
            sorted(
                {
                    (item.start_sample, item.stop_sample)
                    for item in self.points
                    if item.method is REPORT_METHOD_ORDER[0]
                }
            )
        )
        if _union_sample_count(exact_intervals) != self.exact_covered_sample_count:
            raise ValueError("exact covered sample count is inconsistent")
        if not 0 <= self.prescreen_overlap_fraction < 1:
            raise ValueError("prescreen overlap fraction is invalid")
        if not self.refinement_is_data_adaptive:
            raise ValueError("fine refinement dependence must be explicit")
        for point in self.points:
            if (
                point.radio_id != self.radio_id
                or point.segment_id != self.segment_id
                or point.receiver_chain_id != self.receiver_chain_id
                or point.edge != self.edge
            ):
                raise ValueError("point crosses its immutable stream boundary")


@dataclass(frozen=True)
class StarlinkFullDwellResponseBundleV0_1:
    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    source_suite_ref: ArtifactRef
    source_suite_request_digest: Digest
    request_digest: Digest
    search_grid: StarlinkSearchGridV0_1
    plan: StarlinkFullDwellPlanV0_1
    streams: tuple[StarlinkFullDwellStreamResponseV0_1, ...]
    provenance: Provenance
    warnings: tuple[str, ...]
    calibrated_detection_count: None

    SCHEMA_ID = "org.leo-flow.starlink-full-dwell-response-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported full-dwell response schema")
        require_token(self.analysis_id, "analysis_id")
        if not self.analysis_id.startswith("slfd_"):
            raise ValueError("invalid full-dwell response identity")
        keys = tuple(
            (item.segment_id, item.receiver_chain_id, item.edge)
            for item in self.streams
        )
        if keys != tuple(sorted(keys, key=lambda value: tuple(map(str, value)))):
            raise ValueError("full-dwell streams must be canonical")
        if len(keys) != len(set(keys)) or len(keys) > MAXIMUM_FULL_DWELL_STREAMS:
            raise ValueError("full-dwell response streams are duplicate or unbounded")
        score_records = sum(
            1 + len(point.surrogates)
            for stream in self.streams
            for point in stream.points
        )
        if score_records > MAXIMUM_RESPONSE_SCORE_RECORDS:
            raise ValueError("full-dwell response score records exceed their bound")
        if self.calibrated_detection_count is not None:
            raise ValueError("full-dwell responses cannot count detections")
        required = {
            "candidate-evidence-not-calibrated-detection",
            "finite-surrogate-rank-not-p-value",
            "dwell-and-search-look-elsewhere-not-calibrated",
            "overlapping-windows-statistically-dependent",
            "fine-refinement-selected-by-pattern-blind-power",
            "prescreen-window-union-covers-full-dwell",
            "exact-detector-windows-are-selected-not-full-coverage",
        }
        if not self.streams or not required <= set(self.warnings):
            raise ValueError("full-dwell response lacks streams or disclosures")
        if any(
            point.recording_id != self.recording_id
            for stream in self.streams
            for point in stream.points
        ):
            raise ValueError("full-dwell point belongs to another recording")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


def _union_sample_count(intervals: tuple[tuple[int, int], ...]) -> int:
    if not intervals:
        return 0
    total = 0
    start, stop = intervals[0]
    for next_start, next_stop in intervals[1:]:
        if next_start <= stop:
            stop = max(stop, next_stop)
        else:
            total += stop - start
            start, stop = next_start, next_stop
    return total + stop - start


@dataclass(frozen=True)
class StarlinkFullDwellProductRefV0_1:
    analysis_id: str
    recording_id: RecordingId
    bundle_ref: ObjectRef


@dataclass(frozen=True)
class StarlinkFullDwellCatalogProjectionV0_1:
    analysis_id: str
    recording_id: RecordingId
    input_recording_digest: Digest
    source_suite_ref: ArtifactRef
    source_suite_request_digest: Digest
    request_digest: Digest
    stream_count: int
    prescreen_window_count: int
    exact_window_count: int
    point_count: int


@dataclass(frozen=True)
class StarlinkFullDwellQueryV0_1:
    recording_id: RecordingId
    methods: tuple[StarlinkDetectorMethod, ...] = REPORT_METHOD_ORDER
    radio_ids: tuple[RadioId, ...] = ()
    receiver_chain_ids: tuple[ReceiverChainId, ...] = ()
    edges: tuple[StarlinkEdge, ...] = ()
    maximum_points: int = 1024

    def __post_init__(self) -> None:
        for values, label in (
            (self.methods, "methods"),
            (self.radio_ids, "radios"),
            (self.receiver_chain_ids, "receivers"),
            (self.edges, "edges"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"full-dwell query {label} must be unique")
        if not self.methods:
            raise ValueError("full-dwell query requires methods")
        if not 1 <= self.maximum_points <= MAXIMUM_FULL_DWELL_QUERY_POINTS:
            raise ValueError("full-dwell query point bound is invalid")


@dataclass(frozen=True)
class StarlinkFullDwellPresentationStreamV0_1:
    radio_id: RadioId
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int
    prescreen_window_count: int
    exact_window_count: int
    prescreen_coverage_fraction: float
    exact_coverage_fraction: float
    refinement_is_data_adaptive: bool
    points: tuple[StarlinkFullDwellPointV0_1, ...]


@dataclass(frozen=True)
class RecordingStarlinkFullDwellViewV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    analysis_ref: ArtifactRef
    plan: StarlinkFullDwellPlanV0_1
    streams: tuple[StarlinkFullDwellPresentationStreamV0_1, ...]
    original_point_count: int
    truncated: bool
    decimation: str
    queue_state: str
    backlog_depth: int
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.recording-starlink-full-dwell"

    def __post_init__(self) -> None:
        if self.queue_state not in {"complete", "pending", "error", "truncated"}:
            raise ValueError("invalid full-dwell queue state")
        if self.backlog_depth < 0:
            raise ValueError("full-dwell backlog depth must be non-negative")


class RecordingStarlinkFullDwellQueryPortV0_1(Protocol):
    def recording_starlink_full_dwell(
        self, query: StarlinkFullDwellQueryV0_1
    ) -> RecordingStarlinkFullDwellViewV0_1: ...
