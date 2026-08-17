"""Additive public contracts for advanced blind Doppler evidence and lookup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._validation import require_finite, require_token
from .core import (
    ContractId,
    Digest,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
)
from .storage import ObjectRef
from .waterfall import WaterfallProductId

MAX_ADVANCED_TRACKS = 8
MAX_ADVANCED_TRACK_ROWS = 2_048
MAX_SHUFFLE_CONTROLS = 32


class DopplerAnalysisId(ContractId):
    prefix = "doppler"


@dataclass(frozen=True)
class AdvancedTrackEvidenceV0_1:
    path_digest: Digest
    bins: tuple[int, ...]
    slope_bins_per_row: float
    drift_rate_hz_s: float
    score: float
    stationary_improvement: float

    def __post_init__(self) -> None:
        if not 2 <= len(self.bins) <= MAX_ADVANCED_TRACK_ROWS:
            raise ValueError("advanced track row count is outside its bound")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in self.bins
        ):
            raise ValueError("advanced track bins must be nonnegative integers")
        for name in (
            "slope_bins_per_row",
            "drift_rate_hz_s",
            "score",
            "stationary_improvement",
        ):
            require_finite(getattr(self, name), name)


@dataclass(frozen=True)
class SlopeBankEvidenceV0_1:
    candidate_path_digest: Digest
    source_input_digest: Digest
    track: AdvancedTrackEvidenceV0_1
    basic_candidate_rank: int | None
    heldout_score: float
    stationary_score: float
    opposite_slope_score: float
    time_shuffle_scores: tuple[float, ...]
    training_rows: tuple[int, ...]
    validation_rows: tuple[int, ...]
    test_rows: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.basic_candidate_rank is not None and (
            isinstance(self.basic_candidate_rank, bool) or self.basic_candidate_rank < 1
        ):
            raise ValueError("basic candidate rank must be positive when present")
        if not 0 < len(self.time_shuffle_scores) <= MAX_SHUFFLE_CONTROLS:
            raise ValueError("time-shuffle control count is outside its bound")
        for name in ("heldout_score", "stationary_score", "opposite_slope_score"):
            require_finite(getattr(self, name), name)
        for value in self.time_shuffle_scores:
            require_finite(value, "time_shuffle_score")
        splits = (self.training_rows, self.validation_rows, self.test_rows)
        if any(
            not values
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in values
            )
            or tuple(sorted(set(values))) != values
            for values in splits
        ):
            raise ValueError(
                "slope-bank row splits must be nonempty, unique, and sorted"
            )
        flattened = tuple(value for values in splits for value in values)
        if len(flattened) != len(set(flattened)) or set(flattened) != set(
            range(len(self.track.bins))
        ):
            raise ValueError("slope-bank row splits must partition the track rows")


@dataclass(frozen=True)
class CombEvidenceV0_1:
    candidate_path_digest: Digest
    source_input_digest: Digest
    spacing_bins: int
    wrong_spacing_bins: int
    fit_score: float
    heldout_score: float
    wrong_spacing_score: float

    def __post_init__(self) -> None:
        if self.spacing_bins < 1 or self.wrong_spacing_bins < 1:
            raise ValueError("comb spacings must be positive")
        for name in ("fit_score", "heldout_score", "wrong_spacing_score"):
            require_finite(getattr(self, name), name)


@dataclass(frozen=True)
class BroadbandEvidenceV0_1:
    candidate_path_digest: Digest
    source_input_digest: Digest
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
class DualReceiverEvidenceV0_1:
    candidate_path_digest: Digest
    peer_candidate_path_digest: Digest
    source_input_digest: Digest
    peer_receiver_chain_id: ReceiverChainId
    common_slope_bins_per_row: float
    slope_difference: float
    receiver_offsets_bins: tuple[float, float]
    offset_removed_rms_bins: float
    path_correlation: float

    def __post_init__(self) -> None:
        if len(self.receiver_offsets_bins) != 2:
            raise ValueError("dual receiver evidence requires two offsets")
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
class PostBlindTleAssociationV0_1:
    """Optional association result; never used to create blind candidates."""

    candidate_path_digest: Digest
    source_input_digest: Digest
    name: str
    offset_bins: float
    heldout_rms_bins: float
    runner_up_margin_bins: float
    stationary_control_rms_bins: float
    opposite_slope_control_rms_bins: float
    qualified: bool

    def __post_init__(self) -> None:
        require_token(self.name, "tle_name")
        for name in (
            "offset_bins",
            "heldout_rms_bins",
            "runner_up_margin_bins",
            "stationary_control_rms_bins",
            "opposite_slope_control_rms_bins",
        ):
            require_finite(getattr(self, name), name)


@dataclass(frozen=True)
class CandidatePathAssociationV0_1:
    state: str
    candidate_path_digest: Digest
    basic_candidate_rank: int | None
    overlap_point_count: int
    overlap_fraction: float
    mean_frequency_distance_hz: float | None
    maximum_frequency_distance_hz: float | None

    def __post_init__(self) -> None:
        if self.state not in {"matched-basic-candidate", "advanced-path-only"}:
            raise ValueError("unsupported candidate-path association state")
        if isinstance(self.overlap_point_count, bool) or self.overlap_point_count < 0:
            raise ValueError("overlap_point_count must be nonnegative")
        require_finite(self.overlap_fraction, "overlap_fraction")
        if not 0 <= self.overlap_fraction <= 1:
            raise ValueError("overlap_fraction is outside [0,1]")
        distances = (
            self.mean_frequency_distance_hz,
            self.maximum_frequency_distance_hz,
        )
        if any(value is not None and value < 0 for value in distances):
            raise ValueError("candidate-path distances must be nonnegative")
        for value in distances:
            if value is not None:
                require_finite(value, "candidate_path_distance_hz")
        if self.state == "matched-basic-candidate":
            if self.basic_candidate_rank is None or self.basic_candidate_rank < 1:
                raise ValueError("matched association requires a candidate rank")
            if self.overlap_point_count == 0 or any(
                value is None for value in distances
            ):
                raise ValueError("matched association requires overlap and distances")
        elif self.basic_candidate_rank is not None or any(
            value is not None for value in distances
        ):
            raise ValueError("advanced-only association cannot name a basic candidate")


@dataclass(frozen=True)
class AdvancedDopplerEvidenceBundleV0_1:
    schema: SchemaRef
    input_identity_digest: Digest
    blind_bundle_digest: Digest
    config_digest: Digest
    auxiliary_input_digests: tuple[Digest, ...]
    algorithm_version: str
    candidate_only: bool
    spectral_peak_excess_reference: str
    association: CandidatePathAssociationV0_1 | None
    slope_bank: SlopeBankEvidenceV0_1 | None
    peeled_tracks: tuple[AdvancedTrackEvidenceV0_1, ...]
    comb: CombEvidenceV0_1 | None = None
    broadband: BroadbandEvidenceV0_1 | None = None
    dual_receiver: DualReceiverEvidenceV0_1 | None = None
    tle_association: PostBlindTleAssociationV0_1 | None = None
    warnings: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    SCHEMA_ID = "org.leo-flow.advanced-doppler-evidence-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported advanced Doppler evidence schema")
        require_token(self.algorithm_version, "algorithm_version")
        require_token(
            self.spectral_peak_excess_reference, "spectral_peak_excess_reference"
        )
        if self.candidate_only is not True:
            raise ValueError("advanced Doppler evidence is candidate-only")
        if len(self.peeled_tracks) > MAX_ADVANCED_TRACKS:
            raise ValueError("peeled track count is outside its bound")
        if (self.slope_bank is None) != (not self.peeled_tracks):
            raise ValueError(
                "slope-bank and peeled-track evidence must be present together"
            )
        if (self.slope_bank is None) != (self.association is None):
            raise ValueError("candidate association must accompany slope-bank evidence")
        if self.slope_bank is not None and self.association is not None:
            primary = self.association.candidate_path_digest
            if self.slope_bank.candidate_path_digest != primary:
                raise ValueError("slope-bank evidence is bound to another path")
            for optional in (
                self.comb,
                self.broadband,
                self.dual_receiver,
                self.tle_association,
            ):
                if optional is not None and optional.candidate_path_digest != primary:
                    raise ValueError(
                        "advanced evidence items must bind to the primary path"
                    )
        if (
            tuple(sorted(set(self.auxiliary_input_digests), key=str))
            != self.auxiliary_input_digests
        ):
            raise ValueError("auxiliary input digests must be unique and sorted")
        for values, name in (
            (self.warnings, "warning"),
            (self.reason_codes, "reason_code"),
        ):
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"advanced Doppler {name}s must be unique and sorted")
            for value in values:
                require_token(value, name)


@dataclass(frozen=True)
class DopplerAnalysisRefV0_1:
    doppler_id: DopplerAnalysisId
    recording_id: RecordingId
    waterfall_product_id: WaterfallProductId
    waterfall_bundle_digest: Digest
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    spectrogram_digest: Digest
    basic_config_digest: Digest
    advanced_config_digest: Digest
    basic_bundle_ref: ObjectRef
    advanced_bundle_ref: ObjectRef
    candidate_count: int
    moving_candidate_count: int
    strongest_candidate_score: float | None

    def __post_init__(self) -> None:
        for name in ("candidate_count", "moving_candidate_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.moving_candidate_count > self.candidate_count:
            raise ValueError("moving candidates cannot exceed all candidates")
        if self.strongest_candidate_score is not None:
            require_finite(self.strongest_candidate_score, "strongest_candidate_score")
        if (self.candidate_count == 0) != (self.strongest_candidate_score is None):
            raise ValueError("strongest score presence must match candidate count")


class RecordingDopplerAnalysisQueryPortV0_1(Protocol):
    """Dashboard-neutral lookup; callers receive exact immutable blob references."""

    def list_recording_doppler(
        self, recording_id: RecordingId
    ) -> tuple[DopplerAnalysisRefV0_1, ...]: ...
