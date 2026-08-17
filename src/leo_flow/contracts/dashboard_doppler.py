"""Bounded read models for waterfall v0.2 and blind Doppler evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ._validation import require_finite, require_positive, require_token, require_utc_ns
from .core import (
    V0_1,
    Digest,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
    canonical_json_bytes,
)

MAX_DASHBOARD_DOPPLER_TILES = 16
MAX_DASHBOARD_DOPPLER_TIME_BINS = 256
MAX_DASHBOARD_DOPPLER_FREQUENCY_BINS = 512
MAX_DASHBOARD_DOPPLER_PIXELS = 524_288
MAX_DASHBOARD_DOPPLER_JSON_BYTES = 8 * 1024 * 1024
MAX_DASHBOARD_DOPPLER_CANDIDATES = 32
MAX_DASHBOARD_DOPPLER_POINTS = 2_048
MAX_DASHBOARD_DOPPLER_ADVANCED_EVIDENCE = 32


class DopplerVisualizationState(str, Enum):
    UNAVAILABLE = "unavailable"
    PENDING = "pending"
    COMPLETE = "complete"
    FAILED = "failed"


class DopplerTrackModel(str, Enum):
    CONSTANT = "constant"
    LINEAR = "linear"
    QUADRATIC = "quadratic"


class DopplerWaterfallLayer(str, Enum):
    AVERAGE = "average"
    RESIDUAL = "residual"
    HIGH_PERCENTILE = "high-percentile"


class DopplerCandidateAssociationState(str, Enum):
    MATCHED_BASIC_CANDIDATE = "matched-basic-candidate"
    ADVANCED_PATH_ONLY = "advanced-path-only"


@dataclass(frozen=True)
class DopplerProductProvenanceViewV0_1:
    """Public provenance facts without a storage locator or private model."""

    product_kind: str
    artifact_id: str
    schema: SchemaRef
    input_identity_digest: Digest
    algorithm_version: str
    config_digest: Digest | None = None
    analysis_run_id: str | None = None
    producer_name: str | None = None
    producer_version: str | None = None
    git_commit: str | None = None
    started_utc_ns: UtcNs | None = None
    completed_utc_ns: UtcNs | None = None

    def __post_init__(self) -> None:
        for name in ("product_kind", "artifact_id", "algorithm_version"):
            require_token(getattr(self, name), name)
        for name in (
            "analysis_run_id",
            "producer_name",
            "producer_version",
            "git_commit",
        ):
            value = getattr(self, name)
            if value is not None:
                require_token(value, name)
        if (self.started_utc_ns is None) != (self.completed_utc_ns is None):
            raise ValueError(
                "provenance UTC bounds must be both present or both absent"
            )
        if self.started_utc_ns is not None and self.completed_utc_ns is not None:
            require_utc_ns(self.started_utc_ns, "started_utc_ns")
            require_utc_ns(self.completed_utc_ns, "completed_utc_ns")
            if self.completed_utc_ns < self.started_utc_ns:
                raise ValueError("provenance completion precedes start")


@dataclass(frozen=True)
class DopplerTileProvenanceViewV0_1:
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    basic: DopplerProductProvenanceViewV0_1
    advanced: DopplerProductProvenanceViewV0_1 | None = None


@dataclass(frozen=True)
class DopplerWaterfallCoverageViewV0_1:
    contiguous_rf_span_count: int
    contiguous_rf_sample_count: int
    analyzed_sample_count: int
    discarded_tail_sample_count: int
    fft_frame_count: int
    coverage_fraction: float

    def __post_init__(self) -> None:
        for name in (
            "contiguous_rf_span_count",
            "contiguous_rf_sample_count",
            "analyzed_sample_count",
            "discarded_tail_sample_count",
            "fft_frame_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.contiguous_rf_span_count < 1 or self.contiguous_rf_sample_count < 1:
            raise ValueError("waterfall coverage requires RF input")
        if (
            self.analyzed_sample_count + self.discarded_tail_sample_count
            != self.contiguous_rf_sample_count
        ):
            raise ValueError("waterfall coverage sample accounting is inconsistent")
        require_finite(self.coverage_fraction, "coverage_fraction")
        expected = self.analyzed_sample_count / self.contiguous_rf_sample_count
        if abs(self.coverage_fraction - expected) > 1e-12:
            raise ValueError("waterfall coverage fraction is inconsistent")


@dataclass(frozen=True)
class DopplerWaterfallTimeBinViewV0_1:
    midpoint_utc_ns: UtcNs
    power_db: tuple[float, ...]

    def __post_init__(self) -> None:
        require_utc_ns(self.midpoint_utc_ns, "midpoint_utc_ns")
        if not self.power_db:
            raise ValueError("waterfall row must not be empty")
        for value in self.power_db:
            require_finite(value, "waterfall layer value")


@dataclass(frozen=True)
class DopplerWaterfallTileViewV0_1:
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    center_frequency_hz: float
    sample_rate_hz: float
    fft_window_samples: int
    power_reference: str
    high_percentile: float
    frequency_bin_offsets_hz: tuple[float, ...]
    coverage: DopplerWaterfallCoverageViewV0_1
    time_bins: tuple[DopplerWaterfallTimeBinViewV0_1, ...]

    def __post_init__(self) -> None:
        require_positive(self.center_frequency_hz, "center_frequency_hz")
        require_positive(self.sample_rate_hz, "sample_rate_hz")
        require_token(self.power_reference, "power_reference")
        if (
            isinstance(self.fft_window_samples, bool)
            or not isinstance(self.fft_window_samples, int)
            or self.fft_window_samples < 8
        ):
            raise ValueError("fft_window_samples must be an integer of at least eight")
        require_finite(self.high_percentile, "high_percentile")
        if not 50 <= self.high_percentile <= 100:
            raise ValueError("high_percentile must be in [50, 100]")
        columns = len(self.frequency_bin_offsets_hz)
        if not 1 <= columns <= MAX_DASHBOARD_DOPPLER_FREQUENCY_BINS:
            raise ValueError("waterfall frequency-bin count is outside its bound")
        if not 1 <= len(self.time_bins) <= MAX_DASHBOARD_DOPPLER_TIME_BINS:
            raise ValueError("waterfall time-bin count is outside its bound")
        if any(
            later <= earlier
            for earlier, later in zip(
                self.frequency_bin_offsets_hz,
                self.frequency_bin_offsets_hz[1:],
                strict=False,
            )
        ):
            raise ValueError("waterfall frequency offsets must be increasing")
        for offset in self.frequency_bin_offsets_hz:
            require_finite(offset, "frequency_bin_offset_hz")
            if not -self.sample_rate_hz / 2 <= offset < self.sample_rate_hz / 2:
                raise ValueError("waterfall frequency offset is outside Nyquist")
        previous_time = -1
        for row in self.time_bins:
            if len(row.power_db) != columns:
                raise ValueError("waterfall row width differs from its frequency axis")
            if row.midpoint_utc_ns <= previous_time:
                raise ValueError("waterfall rows must have increasing UTC midpoints")
            previous_time = row.midpoint_utc_ns


@dataclass(frozen=True)
class DopplerTrackPointViewV0_1:
    midpoint_utc_ns: UtcNs
    frequency_hz: float
    layer_value_db: float
    local_peak_excess_db: float
    edge_truncated: bool

    def __post_init__(self) -> None:
        require_utc_ns(self.midpoint_utc_ns, "midpoint_utc_ns")
        for name in (
            "frequency_hz",
            "layer_value_db",
            "local_peak_excess_db",
        ):
            require_finite(getattr(self, name), name)


@dataclass(frozen=True)
class DopplerStationaryControlViewV0_1:
    constant_residual_rms_hz: float
    selected_residual_rms_hz: float
    residual_improvement_fraction: float
    bic_margin_over_constant: float
    moving_model_preferred: bool

    def __post_init__(self) -> None:
        for name in (
            "constant_residual_rms_hz",
            "selected_residual_rms_hz",
            "residual_improvement_fraction",
            "bic_margin_over_constant",
        ):
            require_finite(getattr(self, name), name)
        if self.constant_residual_rms_hz < 0 or self.selected_residual_rms_hz < 0:
            raise ValueError("stationary-control residuals must be non-negative")


@dataclass(frozen=True)
class DopplerCandidateViewV0_1:
    rank: int
    component_id: int
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    selected_model: DopplerTrackModel
    reference_utc_ns: UtcNs
    reference_frequency_hz: float
    drift_rate_hz_s: float
    drift_acceleration_hz_s2: float
    residual_rms_hz: float
    robust_scale_hz: float
    inlier_count: int
    mean_spectral_peak_excess_db: float
    peak_layer_value_db: float
    duration_s: float
    missing_row_fraction: float
    edge_truncated_point_count: int
    ranking_score: float
    stationary_control: DopplerStationaryControlViewV0_1
    points: tuple[DopplerTrackPointViewV0_1, ...]

    def __post_init__(self) -> None:
        for name in ("rank", "inlier_count", "edge_truncated_point_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.rank < 1:
            raise ValueError("candidate rank must be positive")
        if (
            isinstance(self.component_id, bool)
            or not isinstance(self.component_id, int)
            or self.component_id < 0
        ):
            raise ValueError("component_id must be a non-negative integer")
        require_utc_ns(self.reference_utc_ns, "reference_utc_ns")
        for name in (
            "reference_frequency_hz",
            "drift_rate_hz_s",
            "drift_acceleration_hz_s2",
            "residual_rms_hz",
            "robust_scale_hz",
            "mean_spectral_peak_excess_db",
            "peak_layer_value_db",
            "duration_s",
            "missing_row_fraction",
            "ranking_score",
        ):
            require_finite(getattr(self, name), name)
        if self.residual_rms_hz < 0 or self.robust_scale_hz < 0 or self.duration_s < 0:
            raise ValueError("candidate scales and duration must be non-negative")
        if not 0 <= self.missing_row_fraction <= 1:
            raise ValueError("missing_row_fraction must be in [0, 1]")
        if not 2 <= len(self.points) <= MAX_DASHBOARD_DOPPLER_POINTS:
            raise ValueError("candidate point count is outside its bound")
        if any(
            later.midpoint_utc_ns <= earlier.midpoint_utc_ns
            for earlier, later in zip(self.points, self.points[1:], strict=False)
        ):
            raise ValueError("candidate points must have increasing UTC midpoints")


@dataclass(frozen=True)
class DopplerCombEvidenceViewV0_1:
    fit_score: float
    heldout_score: float
    wrong_spacing_score: float

    def __post_init__(self) -> None:
        for name in ("fit_score", "heldout_score", "wrong_spacing_score"):
            require_finite(getattr(self, name), name)


@dataclass(frozen=True)
class DopplerBroadbandEvidenceViewV0_1:
    lower_slope_bins_per_row: float
    upper_slope_bins_per_row: float
    edge_slope_difference: float
    width_mad_fraction: float
    texture_shift_bins: float
    texture_correlation: float

    def __post_init__(self) -> None:
        for name in (
            "lower_slope_bins_per_row",
            "upper_slope_bins_per_row",
            "edge_slope_difference",
            "width_mad_fraction",
            "texture_shift_bins",
            "texture_correlation",
        ):
            require_finite(getattr(self, name), name)


@dataclass(frozen=True)
class DopplerDualReceiverEvidenceViewV0_1:
    common_slope_bins_per_row: float
    slope_difference: float
    receiver_offsets_bins: tuple[float, float]
    offset_removed_rms_bins: float
    path_correlation: float

    def __post_init__(self) -> None:
        for name in (
            "common_slope_bins_per_row",
            "slope_difference",
            "offset_removed_rms_bins",
            "path_correlation",
        ):
            require_finite(getattr(self, name), name)
        for value in self.receiver_offsets_bins:
            require_finite(value, "receiver_offset_bins")


@dataclass(frozen=True)
class DopplerOrbitAssociationViewV0_1:
    name: str
    offset_bins: float
    heldout_rms_bins: float
    runner_up_margin_bins: float
    stationary_control_rms_bins: float
    opposite_slope_control_rms_bins: float
    qualified: bool

    def __post_init__(self) -> None:
        require_token(self.name, "name")
        for field_name in (
            "offset_bins",
            "heldout_rms_bins",
            "runner_up_margin_bins",
            "stationary_control_rms_bins",
            "opposite_slope_control_rms_bins",
        ):
            require_finite(getattr(self, field_name), field_name)


@dataclass(frozen=True)
class DopplerCandidatePathAssociationViewV0_1:
    """Explicit post-blind relationship between an advanced path and basic track."""

    state: DopplerCandidateAssociationState
    candidate_path_digest: Digest
    basic_candidate_rank: int | None
    overlap_point_count: int
    overlap_fraction: float
    mean_distance_hz: float | None
    maximum_distance_hz: float | None

    def __post_init__(self) -> None:
        if self.state is DopplerCandidateAssociationState.MATCHED_BASIC_CANDIDATE:
            if (
                isinstance(self.basic_candidate_rank, bool)
                or not isinstance(self.basic_candidate_rank, int)
                or self.basic_candidate_rank < 1
            ):
                raise ValueError(
                    "matched association requires a positive candidate rank"
                )
        elif self.basic_candidate_rank is not None:
            raise ValueError(
                "advanced-path-only association cannot identify a basic rank"
            )
        if (
            isinstance(self.overlap_point_count, bool)
            or not isinstance(self.overlap_point_count, int)
            or self.overlap_point_count < 0
        ):
            raise ValueError("overlap_point_count must be a non-negative integer")
        require_finite(self.overlap_fraction, "overlap_fraction")
        if not 0 <= self.overlap_fraction <= 1:
            raise ValueError("overlap_fraction must be in [0, 1]")
        distances = (self.mean_distance_hz, self.maximum_distance_hz)
        for value in distances:
            if value is not None:
                require_finite(value, "candidate_path_distance_hz")
                if value < 0:
                    raise ValueError("candidate-path distances must be non-negative")
        if self.state is DopplerCandidateAssociationState.MATCHED_BASIC_CANDIDATE:
            if self.overlap_point_count == 0 or any(
                value is None for value in distances
            ):
                raise ValueError("matched association requires overlap and distances")
            assert self.mean_distance_hz is not None
            assert self.maximum_distance_hz is not None
            if self.maximum_distance_hz < self.mean_distance_hz:
                raise ValueError("maximum_distance_hz cannot be below mean_distance_hz")
        elif any(value is not None for value in distances):
            raise ValueError("advanced-path-only association cannot expose distances")


@dataclass(frozen=True)
class DopplerAdvancedEvidenceViewV0_1:
    candidate_rank: int | None
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    slope_bins_per_row: float
    heldout_score: float
    stationary_score: float
    opposite_slope_score: float
    shuffled_scores: tuple[float, ...]
    comb: DopplerCombEvidenceViewV0_1 | None = None
    broadband: DopplerBroadbandEvidenceViewV0_1 | None = None
    dual_receiver: DopplerDualReceiverEvidenceViewV0_1 | None = None
    orbit_association: DopplerOrbitAssociationViewV0_1 | None = None
    drift_rate_hz_s: float | None = None
    spectral_peak_excess_reference: str | None = None
    source_input_digest: Digest | None = None
    candidate_path_digest: Digest | None = None
    association: DopplerCandidatePathAssociationViewV0_1 | None = None

    def __post_init__(self) -> None:
        if self.candidate_rank is not None and (
            isinstance(self.candidate_rank, bool)
            or not isinstance(self.candidate_rank, int)
            or self.candidate_rank < 1
        ):
            raise ValueError("candidate_rank must be positive when present")
        for name in (
            "slope_bins_per_row",
            "heldout_score",
            "stationary_score",
            "opposite_slope_score",
        ):
            require_finite(getattr(self, name), name)
        if not self.shuffled_scores:
            raise ValueError("advanced evidence requires shuffled controls")
        for value in self.shuffled_scores:
            require_finite(value, "shuffled_score")
        if self.drift_rate_hz_s is not None:
            require_finite(self.drift_rate_hz_s, "drift_rate_hz_s")
        if self.spectral_peak_excess_reference is not None:
            require_token(
                self.spectral_peak_excess_reference,
                "spectral_peak_excess_reference",
            )
        if (self.source_input_digest is None) != (self.candidate_path_digest is None):
            raise ValueError("advanced evidence digests must be both present or absent")
        if self.association is not None:
            if (
                self.candidate_path_digest is not None
                and self.association.candidate_path_digest != self.candidate_path_digest
            ):
                raise ValueError("association identifies a different candidate path")
            if (
                self.association.state
                is DopplerCandidateAssociationState.MATCHED_BASIC_CANDIDATE
                and self.association.basic_candidate_rank != self.candidate_rank
            ):
                raise ValueError(
                    "association and advanced evidence candidate ranks differ"
                )
            if (
                self.association.state
                is DopplerCandidateAssociationState.ADVANCED_PATH_ONLY
                and self.candidate_rank is not None
            ):
                raise ValueError(
                    "advanced-path-only evidence cannot identify a basic rank"
                )


@dataclass(frozen=True)
class RecordingDopplerVisualizationViewV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    state: DopplerVisualizationState
    selected_layer: DopplerWaterfallLayer
    candidate_only: bool
    calibrated_detection_count: None
    waterfall_provenance: DopplerProductProvenanceViewV0_1 | None
    doppler_provenance: tuple[DopplerTileProvenanceViewV0_1, ...]
    tiles: tuple[DopplerWaterfallTileViewV0_1, ...]
    candidates: tuple[DopplerCandidateViewV0_1, ...]
    advanced_evidence: tuple[DopplerAdvancedEvidenceViewV0_1, ...]
    warnings: tuple[str, ...]
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.recording-doppler-visualization"
    CANDIDATE_WARNING = "candidate-only-no-calibrated-detection"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported Doppler visualization schema")
        if (
            self.candidate_only is not True
            or self.calibrated_detection_count is not None
        ):
            raise ValueError("blind Doppler visualization must remain candidate-only")
        if self.CANDIDATE_WARNING not in self.warnings:
            raise ValueError("candidate-only warning is required")
        if tuple(sorted(set(self.warnings))) != self.warnings:
            raise ValueError("warnings must be unique and sorted")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("reason_codes must be unique and sorted")
        for value in (*self.warnings, *self.reason_codes):
            require_token(value, "warning_or_reason_code")
        if len(self.tiles) > MAX_DASHBOARD_DOPPLER_TILES:
            raise ValueError("Doppler waterfall tile count exceeds its bound")
        pixels = sum(
            len(tile.time_bins) * len(tile.frequency_bin_offsets_hz)
            for tile in self.tiles
        )
        if pixels > MAX_DASHBOARD_DOPPLER_PIXELS:
            raise ValueError("Doppler waterfall pixels exceed their bound")
        if len(self.candidates) > MAX_DASHBOARD_DOPPLER_CANDIDATES:
            raise ValueError("Doppler candidate count exceeds its bound")
        if len(self.advanced_evidence) > MAX_DASHBOARD_DOPPLER_ADVANCED_EVIDENCE:
            raise ValueError("advanced Doppler evidence count exceeds its bound")
        tile_keys = tuple(
            (tile.segment_id, tile.receiver_chain_id) for tile in self.tiles
        )
        if len(tile_keys) != len(set(tile_keys)):
            raise ValueError("Doppler waterfall tile identities must be unique")
        candidates_by_tile: dict[tuple[SegmentId, ReceiverChainId], list[int]] = {}
        for candidate in self.candidates:
            candidates_by_tile.setdefault(
                (candidate.segment_id, candidate.receiver_chain_id), []
            ).append(candidate.rank)
        if any(
            tuple(ranks) != tuple(range(1, len(ranks) + 1))
            for ranks in candidates_by_tile.values()
        ):
            raise ValueError("Doppler candidate ranks must be contiguous per tile")
        candidate_keys = {
            (candidate.rank, candidate.segment_id, candidate.receiver_chain_id)
            for candidate in self.candidates
        }
        candidate_tile_keys = {
            (candidate.segment_id, candidate.receiver_chain_id)
            for candidate in self.candidates
        }
        if any(key not in set(tile_keys) for key in candidate_tile_keys):
            raise ValueError("Doppler candidates must identify a displayed tile")
        provenance_keys = tuple(
            (item.segment_id, item.receiver_chain_id)
            for item in self.doppler_provenance
        )
        if len(provenance_keys) != len(set(provenance_keys)):
            raise ValueError("Doppler provenance tile identities must be unique")
        if set(provenance_keys) != set(tile_keys):
            raise ValueError("Doppler provenance must identify every analyzed tile")
        advanced_keys = tuple(
            (
                item.candidate_path_digest,
                item.candidate_rank,
                item.segment_id,
                item.receiver_chain_id,
            )
            for item in self.advanced_evidence
        )
        if len(advanced_keys) != len(set(advanced_keys)):
            raise ValueError("advanced evidence identities must be unique")
        matched_advanced_keys = {
            (item.candidate_rank, item.segment_id, item.receiver_chain_id)
            for item in self.advanced_evidence
            if item.candidate_rank is not None
        }
        if any(key not in candidate_keys for key in matched_advanced_keys):
            raise ValueError(
                "matched advanced evidence must identify a displayed candidate"
            )
        advanced_tile_keys = {
            (item.segment_id, item.receiver_chain_id) for item in self.advanced_evidence
        }
        if any(
            item.advanced is None
            for item in self.doppler_provenance
            if (item.segment_id, item.receiver_chain_id) in advanced_tile_keys
        ):
            raise ValueError("advanced evidence requires per-tile advanced provenance")
        if self.state is DopplerVisualizationState.COMPLETE:
            if not self.tiles or self.waterfall_provenance is None:
                raise ValueError("complete visualization requires waterfall data")
        elif self.tiles or self.candidates or self.advanced_evidence:
            raise ValueError("incomplete visualization cannot expose analysis data")
        if len(canonical_json_bytes(self)) > MAX_DASHBOARD_DOPPLER_JSON_BYTES:
            raise ValueError("Doppler visualization JSON exceeds its byte bound")


class RecordingDopplerVisualizationQueryPortV0_1(Protocol):
    """Narrow read port; implementations return dashboard DTOs, never ORM rows."""

    def recording_doppler_visualization(
        self, recording_id: RecordingId, layer: DopplerWaterfallLayer
    ) -> RecordingDopplerVisualizationViewV0_1: ...
