"""Immutable response contract for adaptive, pattern-symmetric pilot searches."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from ._validation import require_finite, require_token
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
from .starlink_adaptive_refinement import (
    StarlinkAdaptiveRefinementPlanV0_1,
    StarlinkAdaptiveRefinementSelectionV0_1,
)
from .starlink_detector_suite import REPORT_METHOD_ORDER, StarlinkDetectorMethod
from .starlink_full_dwell_response import (
    StarlinkFullDwellSurrogateV0_1,
    StarlinkFullDwellWinnerV0_1,
)
from .starlink_surrogate_null import StarlinkSearchGridV0_1
from .storage import ObjectRef, RecordingObjectRef

V0_1 = SchemaVersion(0, 1)
MAXIMUM_ADAPTIVE_STREAMS = 16
MAXIMUM_ADAPTIVE_SCORE_RECORDS = 524_288


@dataclass(frozen=True)
class StarlinkAdaptivePowerSeedV0_1:
    rank: int
    start_sample: int
    stop_sample: int

    def __post_init__(self) -> None:
        if (
            self.rank < 0
            or self.start_sample < 0
            or self.stop_sample <= self.start_sample
        ):
            raise ValueError("adaptive power seed geometry is invalid")


@dataclass(frozen=True)
class StarlinkAdaptiveStreamSelectionV0_1:
    radio_id: RadioId
    lnb_id: str
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int
    power_seeds: tuple[StarlinkAdaptivePowerSeedV0_1, ...]

    def __post_init__(self) -> None:
        require_token(self.lnb_id, "lnb_id")
        require_finite(self.sample_rate_hz, "sample_rate_hz")
        if (
            self.channel_number not in (1, 2, 3, 4)
            or self.sample_rate_hz <= 0
            or self.segment_sample_count <= 0
        ):
            raise ValueError("adaptive stream geometry is invalid")
        ranks = tuple(item.rank for item in self.power_seeds)
        if ranks != tuple(range(len(ranks))):
            raise ValueError("adaptive power-seed ranks must be canonical")
        if any(
            item.stop_sample > self.segment_sample_count for item in self.power_seeds
        ):
            raise ValueError("adaptive power seed exceeds its segment")

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
class StarlinkAdaptiveResponseRequestV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    timeline_ref: ArtifactRef
    timeline_request_digest: Digest
    source_suite_ref: ArtifactRef
    source_suite_request_digest: Digest
    search_grid: StarlinkSearchGridV0_1
    plan: StarlinkAdaptiveRefinementPlanV0_1
    streams: tuple[StarlinkAdaptiveStreamSelectionV0_1, ...]
    surrogate_count: int
    requested_output_schema: SchemaRef

    SCHEMA_ID = "org.leo-flow.starlink-adaptive-response-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported adaptive response request schema")
        if self.requested_output_schema != SchemaRef(
            StarlinkAdaptiveResponseBundleV0_1.SCHEMA_ID, V0_1
        ):
            raise ValueError("unsupported adaptive response output schema")
        if self.recording_id != self.recording_object_ref.recording_id:
            raise ValueError("adaptive response recording identities differ")
        identities = tuple(item.identity for item in self.streams)
        if (
            not identities
            or identities != tuple(sorted(identities))
            or len(identities) != len(set(identities))
            or len(identities) > MAXIMUM_ADAPTIVE_STREAMS
        ):
            raise ValueError("adaptive response streams are invalid")
        if not 1 <= self.surrogate_count <= 32:
            raise ValueError("adaptive response surrogate count is invalid")
        if any(
            len(item.power_seeds) > self.plan.maximum_power_seeds
            or item.segment_sample_count < self.plan.probe_sample_count
            for item in self.streams
        ):
            raise ValueError("adaptive response stream exceeds its plan")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkAdaptiveResponsePointV0_1:
    method: StarlinkDetectorMethod
    window_index: int
    start_sample: int
    stop_sample: int
    interval_start_utc_ns: UtcNs
    interval_stop_utc_ns: UtcNs
    qin: StarlinkFullDwellWinnerV0_1
    surrogates: tuple[StarlinkFullDwellSurrogateV0_1, ...]
    finite_upper_tail_rank: int
    qin_minus_max_surrogate: float

    def __post_init__(self) -> None:
        if (
            self.window_index < 0
            or self.start_sample < 0
            or self.stop_sample <= self.start_sample
            or self.interval_stop_utc_ns <= self.interval_start_utc_ns
        ):
            raise ValueError("adaptive response point interval is invalid")
        indices = tuple(item.codebook_index for item in self.surrogates)
        if not indices or indices != tuple(range(len(indices))):
            raise ValueError("adaptive response surrogates are noncanonical")
        expected_rank = 1 + sum(
            item.winner.score >= self.qin.score for item in self.surrogates
        )
        expected_margin = self.qin.score - max(
            item.winner.score for item in self.surrogates
        )
        if self.finite_upper_tail_rank != expected_rank or not math.isclose(
            self.qin_minus_max_surrogate, expected_margin, abs_tol=1e-15
        ):
            raise ValueError("adaptive paired score summary is inconsistent")
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
            raise ValueError("adaptive Qin/surrogate search geometries differ")


@dataclass(frozen=True)
class StarlinkAdaptiveResponseStreamV0_1:
    radio_id: RadioId
    lnb_id: str
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int
    selection: StarlinkAdaptiveRefinementSelectionV0_1
    points: tuple[StarlinkAdaptiveResponsePointV0_1, ...]
    exact_covered_sample_count: int
    exact_coverage_fraction: float

    def __post_init__(self) -> None:
        require_token(self.lnb_id, "lnb_id")
        expected = tuple(
            (window.window_index, method)
            for window in self.selection.exact_windows
            for method in REPORT_METHOD_ORDER
        )
        actual = tuple((point.window_index, point.method) for point in self.points)
        if expected != actual:
            raise ValueError("adaptive stream must contain every method/window")
        union = _union_sample_count(
            tuple(
                (item.start_sample, item.stop_sample)
                for item in self.selection.exact_windows
            )
        )
        if union != self.exact_covered_sample_count or not math.isclose(
            self.exact_coverage_fraction,
            union / self.segment_sample_count,
            abs_tol=1e-15,
        ):
            raise ValueError("adaptive stream coverage is inconsistent")

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
class StarlinkAdaptiveResponseBundleV0_1:
    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    timeline_ref: ArtifactRef
    source_suite_ref: ArtifactRef
    request_digest: Digest
    search_grid: StarlinkSearchGridV0_1
    plan: StarlinkAdaptiveRefinementPlanV0_1
    streams: tuple[StarlinkAdaptiveResponseStreamV0_1, ...]
    provenance: Provenance
    warnings: tuple[str, ...]
    calibrated_detection_count: None

    SCHEMA_ID = "org.leo-flow.starlink-adaptive-response-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported adaptive response bundle schema")
        require_token(self.analysis_id, "analysis_id")
        if not self.analysis_id.startswith("slar_"):
            raise ValueError("invalid adaptive response identity")
        identities = tuple(item.identity for item in self.streams)
        if (
            not identities
            or identities != tuple(sorted(identities))
            or len(identities) != len(set(identities))
        ):
            raise ValueError("adaptive response streams are noncanonical")
        score_records = sum(
            1 + len(point.surrogates)
            for stream in self.streams
            for point in stream.points
        )
        if score_records > MAXIMUM_ADAPTIVE_SCORE_RECORDS:
            raise ValueError("adaptive response exceeds its score-record bound")
        required = {
            "candidate-evidence-not-calibrated-detection",
            "finite-surrogate-rank-not-p-value",
            "time-look-elsewhere-calibration-required",
            "base-sentinels-span-dwell-but-do-not-cover-every-sample",
            "all-patterns-search-the-union-of-selected-local-windows",
            "exact-window-union-is-sparse-and-dependent",
        }
        if self.calibrated_detection_count is not None or not required <= set(
            self.warnings
        ):
            raise ValueError("adaptive response disclosures are incomplete")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkAdaptiveResponseProductRefV0_1:
    analysis_id: str
    recording_id: RecordingId
    bundle_ref: ObjectRef

    @property
    def artifact_ref(self) -> ArtifactRef:
        return ArtifactRef(
            self.analysis_id,
            self.bundle_ref.digest,
            SchemaRef(StarlinkAdaptiveResponseBundleV0_1.SCHEMA_ID, V0_1),
        )


@dataclass(frozen=True)
class StarlinkAdaptiveResponseCatalogProjectionV0_1:
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    timeline_ref: ArtifactRef
    source_suite_ref: ArtifactRef
    request_digest: Digest
    stream_count: int
    window_count: int
    point_count: int

    def __post_init__(self) -> None:
        require_token(self.analysis_id, "analysis_id")
        if (
            not 1 <= self.stream_count <= MAXIMUM_ADAPTIVE_STREAMS
            or not self.stream_count <= self.window_count
            or self.point_count != self.window_count * len(REPORT_METHOD_ORDER)
        ):
            raise ValueError("adaptive response projection counts are invalid")


@dataclass(frozen=True)
class StarlinkAdaptiveResponseQueryV0_1:
    recording_id: RecordingId
    methods: tuple[StarlinkDetectorMethod, ...] = REPORT_METHOD_ORDER
    radio_ids: tuple[RadioId, ...] = ()
    lnb_ids: tuple[str, ...] = ()
    receiver_chain_ids: tuple[ReceiverChainId, ...] = ()
    edges: tuple[StarlinkEdge, ...] = ()
    maximum_points: int = 4096

    def __post_init__(self) -> None:
        for values, label in (
            (self.methods, "methods"),
            (self.radio_ids, "radios"),
            (self.lnb_ids, "LNBs"),
            (self.receiver_chain_ids, "receivers"),
            (self.edges, "edges"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"adaptive query {label} must be unique")
        if not self.methods or not 1 <= self.maximum_points <= 16_384:
            raise ValueError("adaptive query bounds are invalid")


@dataclass(frozen=True)
class StarlinkAdaptiveResponsePresentationStreamV0_1:
    radio_id: RadioId
    lnb_id: str
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int
    selection: StarlinkAdaptiveRefinementSelectionV0_1
    points: tuple[StarlinkAdaptiveResponsePointV0_1, ...]
    exact_coverage_fraction: float


@dataclass(frozen=True)
class RecordingStarlinkAdaptiveResponseViewV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    analysis_ref: ArtifactRef
    timeline_ref: ArtifactRef
    plan: StarlinkAdaptiveRefinementPlanV0_1
    streams: tuple[StarlinkAdaptiveResponsePresentationStreamV0_1, ...]
    original_point_count: int
    truncated: bool
    decimation: str
    candidate_only: bool
    calibration_required: bool
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.recording-starlink-adaptive-response"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported adaptive response dashboard schema")
        if self.original_point_count < sum(len(item.points) for item in self.streams):
            raise ValueError("adaptive response presentation count is invalid")
        if not self.candidate_only or not self.calibration_required:
            raise ValueError("adaptive response dashboard must remain fail-closed")


class RecordingStarlinkAdaptiveResponseQueryPortV0_1(Protocol):
    def recording_starlink_adaptive_response(
        self, query: StarlinkAdaptiveResponseQueryV0_1
    ) -> RecordingStarlinkAdaptiveResponseViewV0_1: ...


def _union_sample_count(intervals: tuple[tuple[int, int], ...]) -> int:
    if not intervals:
        return 0
    ordered = sorted(intervals)
    total = 0
    start, stop = ordered[0]
    for next_start, next_stop in ordered[1:]:
        if next_start <= stop:
            stop = max(stop, next_stop)
        else:
            total += stop - start
            start, stop = next_start, next_stop
    return total + stop - start
