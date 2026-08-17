"""Additive v0.1 contracts for stratified temporal pilot candidate evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass
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
MAXIMUM_TEMPORAL_PROBES = 64
MAXIMUM_TEMPORAL_STREAMS = 16
MAXIMUM_TEMPORAL_QUERY_POINTS = 4096


@dataclass(frozen=True)
class StarlinkTemporalProbePlanV0_1:
    """Bounded temporal sampling plan; coverage is reported, never implied."""

    window_sample_count: int
    nominal_stride_samples: int
    maximum_probe_count: int
    surrogate_count: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.window_sample_count, "window_sample_count"),
            (self.nominal_stride_samples, "nominal_stride_samples"),
            (self.maximum_probe_count, "maximum_probe_count"),
            (self.surrogate_count, "surrogate_count"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        if self.maximum_probe_count > MAXIMUM_TEMPORAL_PROBES:
            raise ValueError("temporal probe count exceeds its bound")
        if self.surrogate_count > MAXIMUM_SURROGATES:
            raise ValueError("temporal surrogate count exceeds its bound")

    @property
    def overlap_fraction(self) -> float:
        return max(
            0.0,
            (self.window_sample_count - self.nominal_stride_samples)
            / self.window_sample_count,
        )


@dataclass(frozen=True)
class StarlinkTemporalStreamSelectionV0_1:
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int

    def __post_init__(self) -> None:
        require_finite(self.sample_rate_hz, "sample_rate_hz")
        if self.sample_rate_hz <= 0:
            raise ValueError("temporal sample rate must be positive")
        if (
            isinstance(self.segment_sample_count, bool)
            or not isinstance(self.segment_sample_count, int)
            or self.segment_sample_count <= 0
        ):
            raise ValueError("temporal segment sample count must be positive")


@dataclass(frozen=True)
class StarlinkTemporalPilotRequestV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    source_suite_ref: ArtifactRef
    source_suite_request_digest: Digest
    search_grid: StarlinkSearchGridV0_1
    plan: StarlinkTemporalProbePlanV0_1
    stream_selections: tuple[StarlinkTemporalStreamSelectionV0_1, ...]
    requested_output_schema: SchemaRef
    ineligible_reason: str | None = None

    SCHEMA_ID = "org.leo-flow.starlink-temporal-pilot-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported temporal request schema")
        if self.recording_id != self.recording_object_ref.recording_id:
            raise ValueError("temporal request recording identities differ")
        if self.requested_output_schema != SchemaRef(
            StarlinkTemporalPilotRecordingBundleV0_1.SCHEMA_ID, V0_1
        ):
            raise ValueError("unsupported temporal output schema")
        keys = tuple(
            (item.segment_id, item.receiver_chain_id) for item in self.stream_selections
        )
        if keys != tuple(sorted(keys, key=lambda item: tuple(map(str, item)))):
            raise ValueError("temporal stream selections must be canonical")
        if len(keys) != len(set(keys)) or len(keys) > MAXIMUM_TEMPORAL_STREAMS:
            raise ValueError("temporal stream selections are duplicate or unbounded")
        if self.ineligible_reason is None:
            if not keys:
                raise ValueError("eligible temporal request requires streams")
            if any(
                item.segment_sample_count < self.plan.window_sample_count
                for item in self.stream_selections
            ):
                raise ValueError("temporal window exceeds a selected segment")
        elif self.ineligible_reason != "clipped-pilot-band" or keys:
            raise ValueError("invalid temporal ineligibility")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkTemporalWinnerV0_1:
    score: float
    winning_epoch_sample: int
    winning_coarse_cfo_hz: float
    winning_residual_cfo_hz: float
    effective_search_cell_count: int
    search_mode: StarlinkPatternSearchMode

    def __post_init__(self) -> None:
        for field in ("score", "winning_coarse_cfo_hz", "winning_residual_cfo_hz"):
            require_finite(getattr(self, field), field)
        if not 0 <= self.score <= 1:
            raise ValueError("temporal score must lie in [0,1]")
        if self.winning_epoch_sample < 0 or self.effective_search_cell_count <= 0:
            raise ValueError("temporal winner has invalid search coordinates")


@dataclass(frozen=True)
class StarlinkTemporalSurrogateWinnerV0_1:
    codebook_index: int
    template_digest: Digest
    winner: StarlinkTemporalWinnerV0_1

    def __post_init__(self) -> None:
        if not 0 <= self.codebook_index < MAXIMUM_SURROGATES:
            raise ValueError("surrogate codebook index is out of bounds")


@dataclass(frozen=True)
class StarlinkTemporalMethodPointV0_1:
    probe_index: int
    start_sample: int
    stop_sample: int
    center_sample: float
    interval_start_utc_ns: UtcNs
    interval_stop_utc_ns: UtcNs
    method: StarlinkDetectorMethod
    qin: StarlinkTemporalWinnerV0_1
    surrogates: tuple[StarlinkTemporalSurrogateWinnerV0_1, ...]
    finite_upper_tail_rank: int
    qin_minus_max_surrogate: float

    def __post_init__(self) -> None:
        if self.probe_index < 0 or self.start_sample < 0:
            raise ValueError("temporal probe coordinates must be non-negative")
        if self.stop_sample <= self.start_sample:
            raise ValueError("temporal probe interval must be non-empty")
        if self.center_sample != (self.start_sample + self.stop_sample) / 2:
            raise ValueError("temporal probe center is inconsistent")
        require_utc_ns(self.interval_start_utc_ns, "interval_start_utc_ns")
        require_utc_ns(self.interval_stop_utc_ns, "interval_stop_utc_ns")
        if self.interval_stop_utc_ns <= self.interval_start_utc_ns:
            raise ValueError("temporal UTC interval must be non-empty")
        indices = tuple(item.codebook_index for item in self.surrogates)
        if indices != tuple(range(len(indices))):
            raise ValueError("temporal surrogates must be canonical")
        expected_rank = 1 + sum(
            item.winner.score >= self.qin.score for item in self.surrogates
        )
        if self.finite_upper_tail_rank != expected_rank:
            raise ValueError("finite temporal rank is inconsistent")
        expected_margin = self.qin.score - max(
            item.winner.score for item in self.surrogates
        )
        if not math.isclose(
            self.qin_minus_max_surrogate, expected_margin, abs_tol=1e-15
        ):
            raise ValueError("temporal paired margin is inconsistent")


@dataclass(frozen=True)
class StarlinkTemporalDwellMethodSummaryV0_1:
    method: StarlinkDetectorMethod
    qin_maximum: float
    surrogate_maxima: tuple[float, ...]
    finite_upper_tail_rank: int
    qin_minus_max_surrogate: float
    candidate_window_count: int
    probe_count: int

    def __post_init__(self) -> None:
        scores = (self.qin_maximum, *self.surrogate_maxima)
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in scores):
            raise ValueError("dwell maximum scores must be finite in [0,1]")
        if not 1 <= len(self.surrogate_maxima) <= MAXIMUM_SURROGATES:
            raise ValueError("dwell summary requires bounded surrogates")
        if self.finite_upper_tail_rank != 1 + sum(
            value >= self.qin_maximum for value in self.surrogate_maxima
        ):
            raise ValueError("dwell finite rank is inconsistent")
        if not math.isclose(
            self.qin_minus_max_surrogate,
            self.qin_maximum - max(self.surrogate_maxima),
            abs_tol=1e-15,
        ):
            raise ValueError("dwell margin is inconsistent")
        if not 0 <= self.candidate_window_count <= self.probe_count:
            raise ValueError("candidate occupancy count is inconsistent")

    @property
    def candidate_occupancy_fraction(self) -> float:
        return self.candidate_window_count / self.probe_count


@dataclass(frozen=True)
class StarlinkTemporalStreamEvidenceV0_1:
    radio_id: RadioId
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int
    probe_starts: tuple[int, ...]
    points: tuple[StarlinkTemporalMethodPointV0_1, ...]
    dwell_summaries: tuple[StarlinkTemporalDwellMethodSummaryV0_1, ...]
    analyzed_sample_count: int
    coverage_fraction: float

    def __post_init__(self) -> None:
        if self.channel_number not in (1, 2, 3, 4):
            raise ValueError("temporal channel must be one of 1,2,3,4")
        if not self.probe_starts or len(self.probe_starts) > MAXIMUM_TEMPORAL_PROBES:
            raise ValueError("temporal probe starts are empty or unbounded")
        if self.probe_starts != tuple(sorted(set(self.probe_starts))):
            raise ValueError("temporal probe starts must be canonical")
        expected = tuple(
            (probe, method)
            for probe in range(len(self.probe_starts))
            for method in REPORT_METHOD_ORDER
        )
        actual = tuple((point.probe_index, point.method) for point in self.points)
        if actual != expected:
            raise ValueError("temporal points must cover every probe and method")
        if tuple(item.method for item in self.dwell_summaries) != REPORT_METHOD_ORDER:
            raise ValueError("temporal dwell summaries must cover every method")
        if not 0 < self.analyzed_sample_count <= self.segment_sample_count:
            raise ValueError("temporal analyzed sample count is invalid")
        expected_fraction = self.analyzed_sample_count / self.segment_sample_count
        if not math.isclose(self.coverage_fraction, expected_fraction, abs_tol=1e-15):
            raise ValueError("temporal coverage fraction is inconsistent")


@dataclass(frozen=True)
class StarlinkTemporalPilotRecordingBundleV0_1:
    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    source_suite_ref: ArtifactRef
    source_suite_request_digest: Digest
    request_digest: Digest
    search_grid: StarlinkSearchGridV0_1
    plan: StarlinkTemporalProbePlanV0_1
    streams: tuple[StarlinkTemporalStreamEvidenceV0_1, ...]
    provenance: Provenance
    warnings: tuple[str, ...]
    calibrated_detection_count: int | None

    SCHEMA_ID = "org.leo-flow.starlink-temporal-pilot-recording-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported temporal recording schema")
        require_token(self.analysis_id, "analysis_id")
        if not self.analysis_id.startswith("sltime_"):
            raise ValueError("invalid temporal analysis identity")
        keys = tuple((item.segment_id, item.receiver_chain_id) for item in self.streams)
        if keys != tuple(sorted(keys, key=lambda item: tuple(map(str, item)))):
            raise ValueError("temporal streams must be canonical")
        if len(keys) != len(set(keys)) or len(keys) > MAXIMUM_TEMPORAL_STREAMS:
            raise ValueError("temporal streams are duplicate or unbounded")
        if self.calibrated_detection_count is not None:
            raise ValueError("temporal evidence cannot count detections")
        required = {
            "candidate-evidence-not-calibrated-detection",
            "finite-surrogate-rank-not-p-value",
            "dwell-maxima-include-time-look-elsewhere",
            "overlapping-windows-statistically-dependent",
        }
        if self.streams:
            if not required <= set(self.warnings):
                raise ValueError("temporal bundle lacks required disclosures")
        elif self.warnings != ("clipped-pilot-band",):
            raise ValueError("empty temporal bundle must explain ineligibility")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkTemporalPilotProductRefV0_1:
    analysis_id: str
    recording_id: RecordingId
    bundle_ref: ObjectRef


@dataclass(frozen=True)
class StarlinkTemporalPilotCatalogProjectionV0_1:
    analysis_id: str
    recording_id: RecordingId
    input_recording_digest: Digest
    request_digest: Digest
    source_suite_ref: ArtifactRef
    source_suite_request_digest: Digest
    stream_count: int
    probe_count: int
    point_count: int


@dataclass(frozen=True)
class StarlinkTemporalPilotQueryV0_1:
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
                raise ValueError(f"temporal query {label} must be unique")
        if not self.methods:
            raise ValueError("temporal query requires methods")
        if not 1 <= self.maximum_points <= MAXIMUM_TEMPORAL_QUERY_POINTS:
            raise ValueError("temporal query point bound is invalid")


@dataclass(frozen=True)
class StarlinkTemporalPresentationStreamV0_1:
    radio_id: RadioId
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int
    analyzed_sample_count: int
    coverage_fraction: float
    points: tuple[StarlinkTemporalMethodPointV0_1, ...]
    dwell_summaries: tuple[StarlinkTemporalDwellMethodSummaryV0_1, ...]


@dataclass(frozen=True)
class RecordingStarlinkTemporalPilotViewV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    analysis_ref: ArtifactRef
    plan: StarlinkTemporalProbePlanV0_1
    streams: tuple[StarlinkTemporalPresentationStreamV0_1, ...]
    original_point_count: int
    truncated: bool
    decimation: str
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.recording-starlink-temporal-pilot"


class RecordingStarlinkTemporalPilotQueryPortV0_1(Protocol):
    def recording_starlink_temporal_pilot(
        self, query: StarlinkTemporalPilotQueryV0_1
    ) -> RecordingStarlinkTemporalPilotViewV0_1: ...
