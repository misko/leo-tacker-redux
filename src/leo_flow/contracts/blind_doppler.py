"""Versioned, bounded contracts for candidate-only blind Doppler tracking."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol

from ._validation import require_finite, require_positive, require_token, require_utc_ns
from .core import Digest, ReceiverChainId, SchemaRef, SegmentId, UtcNs

MAX_SPECTROGRAM_ROWS = 2_048
MAX_SPECTROGRAM_BINS = 2_048
MAX_SPECTROGRAM_CELLS = 1_048_576
MAX_DOPPLER_CANDIDATES = 32
MAX_TRACK_POINTS = MAX_SPECTROGRAM_ROWS


class DopplerPolynomialOrder(IntEnum):
    CONSTANT = 0
    LINEAR = 1
    QUADRATIC = 2


@dataclass(frozen=True)
class SpectrogramRowV0_1:
    """One time-ordered row from any public spectrogram producer."""

    midpoint_utc_ns: UtcNs
    power_db: tuple[float, ...]

    def __post_init__(self) -> None:
        require_utc_ns(self.midpoint_utc_ns, "midpoint_utc_ns")
        if not self.power_db:
            raise ValueError("spectrogram row requires at least one frequency bin")
        for value in self.power_db:
            require_finite(value, "power_db")


@dataclass(frozen=True)
class SpectrogramSliceV0_1:
    """Producer-neutral spectrogram input; it is not a waterfall implementation type."""

    schema: SchemaRef
    input_identity_digest: Digest
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    center_frequency_hz: float
    frequency_bin_offsets_hz: tuple[float, ...]
    power_reference: str
    rows: tuple[SpectrogramRowV0_1, ...]

    SCHEMA_ID = "org.leo-flow.blind-doppler-spectrogram-input"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported blind Doppler spectrogram schema")
        require_positive(self.center_frequency_hz, "center_frequency_hz")
        require_token(self.power_reference, "power_reference")
        bins = len(self.frequency_bin_offsets_hz)
        if not 2 < bins <= MAX_SPECTROGRAM_BINS:
            raise ValueError("spectrogram frequency bin count is outside its bound")
        for value in self.frequency_bin_offsets_hz:
            require_finite(value, "frequency_bin_offset_hz")
        if any(
            b <= a
            for a, b in zip(
                self.frequency_bin_offsets_hz,
                self.frequency_bin_offsets_hz[1:],
                strict=False,
            )
        ):
            raise ValueError(
                "spectrogram frequency offsets must be strictly increasing"
            )
        if not 1 < len(self.rows) <= MAX_SPECTROGRAM_ROWS:
            raise ValueError("spectrogram row count is outside its bound")
        if len(self.rows) * bins > MAX_SPECTROGRAM_CELLS:
            raise ValueError("spectrogram cell count exceeds its bound")
        previous_time = -1
        for row in self.rows:
            if len(row.power_db) != bins:
                raise ValueError("spectrogram power row and frequency axis differ")
            if row.midpoint_utc_ns <= previous_time:
                raise ValueError(
                    "spectrogram rows must be in strictly increasing time order"
                )
            previous_time = row.midpoint_utc_ns


@dataclass(frozen=True)
class BlindDopplerAnalysisRequestV0_1:
    schema: SchemaRef
    input_identity_digest: Digest
    config_digest: Digest
    max_candidates: int

    SCHEMA_ID = "org.leo-flow.blind-doppler-analysis-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported blind Doppler request schema")
        if (
            isinstance(self.max_candidates, bool)
            or not isinstance(self.max_candidates, int)
            or not 0 < self.max_candidates <= MAX_DOPPLER_CANDIDATES
        ):
            raise ValueError("max_candidates is outside its bound")


@dataclass(frozen=True)
class DopplerTrackPointV0_1:
    row_index: int
    midpoint_utc_ns: UtcNs
    frequency_hz: float
    interpolated_bin: float
    layer_value_db: float
    row_baseline_db: float
    local_peak_excess_db: float
    edge_truncated: bool

    def __post_init__(self) -> None:
        if (
            isinstance(self.row_index, bool)
            or not isinstance(self.row_index, int)
            or self.row_index < 0
        ):
            raise ValueError("row_index must be a nonnegative integer")
        require_utc_ns(self.midpoint_utc_ns, "midpoint_utc_ns")
        for name in (
            "frequency_hz",
            "interpolated_bin",
            "layer_value_db",
            "row_baseline_db",
            "local_peak_excess_db",
        ):
            require_finite(getattr(self, name), name)


@dataclass(frozen=True)
class DopplerPolynomialFitV0_1:
    """Fit uses f(dt)=f0 + rate*dt + 0.5*acceleration*dt^2."""

    order: DopplerPolynomialOrder
    reference_utc_ns: UtcNs
    frequency_hz: float
    drift_rate_hz_s: float
    drift_acceleration_hz_s2: float
    residual_rms_hz: float
    robust_scale_hz: float
    inlier_count: int
    bic: float

    def __post_init__(self) -> None:
        require_utc_ns(self.reference_utc_ns, "reference_utc_ns")
        for name in (
            "frequency_hz",
            "drift_rate_hz_s",
            "drift_acceleration_hz_s2",
            "residual_rms_hz",
            "robust_scale_hz",
            "bic",
        ):
            require_finite(getattr(self, name), name)
        if self.residual_rms_hz < 0 or self.robust_scale_hz < 0:
            raise ValueError("fit residual scales must be nonnegative")
        if (
            isinstance(self.inlier_count, bool)
            or not isinstance(self.inlier_count, int)
            or self.inlier_count < 0
        ):
            raise ValueError("inlier_count must be a nonnegative integer")
        if self.order is DopplerPolynomialOrder.CONSTANT and (
            self.drift_rate_hz_s != 0 or self.drift_acceleration_hz_s2 != 0
        ):
            raise ValueError("constant fit contains motion coefficients")
        if (
            self.order is DopplerPolynomialOrder.LINEAR
            and self.drift_acceleration_hz_s2 != 0
        ):
            raise ValueError("linear fit contains acceleration")


@dataclass(frozen=True)
class StationaryControlEvidenceV0_1:
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
            raise ValueError("stationary control residuals must be nonnegative")


@dataclass(frozen=True)
class BlindDopplerCandidateV0_1:
    rank: int
    component_id: int
    points: tuple[DopplerTrackPointV0_1, ...]
    fits: tuple[DopplerPolynomialFitV0_1, ...]
    selected_order: DopplerPolynomialOrder
    stationary_control: StationaryControlEvidenceV0_1
    mean_spectral_peak_excess_db: float
    peak_layer_value_db: float
    duration_s: float
    missing_row_count: int
    missing_row_fraction: float
    edge_truncated_point_count: int
    ranking_score: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.rank, bool)
            or not isinstance(self.rank, int)
            or self.rank < 1
        ):
            raise ValueError("candidate rank must be positive")
        if (
            isinstance(self.component_id, bool)
            or not isinstance(self.component_id, int)
            or self.component_id < 0
        ):
            raise ValueError("component_id must be nonnegative")
        if not 2 <= len(self.points) <= MAX_TRACK_POINTS:
            raise ValueError("candidate point count is outside its bound")
        if any(
            b.row_index <= a.row_index
            for a, b in zip(self.points, self.points[1:], strict=False)
        ):
            raise ValueError("candidate points must have increasing row indices")
        orders = tuple(fit.order for fit in self.fits)
        if orders != tuple(sorted(set(orders))):
            raise ValueError("candidate fits must be unique and ordered")
        if self.selected_order not in orders:
            raise ValueError("selected fit is absent")
        for name in (
            "mean_spectral_peak_excess_db",
            "peak_layer_value_db",
            "duration_s",
            "missing_row_fraction",
            "ranking_score",
        ):
            require_finite(getattr(self, name), name)
        if self.duration_s < 0 or not 0 <= self.missing_row_fraction <= 1:
            raise ValueError("candidate duration or missing fraction is invalid")
        for name in ("missing_row_count", "edge_truncated_point_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")


@dataclass(frozen=True)
class BlindDopplerBundleV0_1:
    schema: SchemaRef
    input_identity_digest: Digest
    config_digest: Digest
    algorithm_version: str
    candidate_only: bool
    examined_row_count: int
    extracted_peak_count: int
    candidates: tuple[BlindDopplerCandidateV0_1, ...]
    warnings: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    SCHEMA_ID = "org.leo-flow.blind-doppler-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported blind Doppler bundle schema")
        require_token(self.algorithm_version, "algorithm_version")
        if self.candidate_only is not True:
            raise ValueError("blind Doppler v0.1 outputs are candidate-only")
        for name in ("examined_row_count", "extracted_peak_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if len(self.candidates) > MAX_DOPPLER_CANDIDATES:
            raise ValueError("candidate count exceeds its bound")
        if tuple(item.rank for item in self.candidates) != tuple(
            range(1, len(self.candidates) + 1)
        ):
            raise ValueError("candidate ranks must be contiguous")


class BlindDopplerSpectrogramPortV0_1(Protocol):
    """Narrow source port implemented by adapters, never by tracker internals."""

    def read_spectrogram(
        self, request: BlindDopplerAnalysisRequestV0_1
    ) -> SpectrogramSliceV0_1: ...


class BlindDopplerAnalyzerV0_1(Protocol):
    def analyze_blind_doppler(
        self,
        spectrogram: SpectrogramSliceV0_1,
        request: BlindDopplerAnalysisRequestV0_1,
    ) -> BlindDopplerBundleV0_1: ...
