"""Application-neutral waterfall v0.2 to blind/advanced Doppler pipeline."""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from leo_flow.analysis.doppler_evidence import (
    LinearTrack,
    associate_tle_post_blind,
    broadband_motion_support,
    comb_support,
    dedoppler_slope_bank,
    dual_receiver_consensus,
    viterbi_peel_tracks,
)
from leo_flow.analysis.tracking.blind_doppler import (
    BasicBlindDopplerAnalyzer,
    BlindDopplerConfig,
    blind_doppler_config_digest,
)
from leo_flow.analysis.tracking.blind_doppler_codec import (
    encode_blind_doppler_bundle,
)
from leo_flow.contracts.blind_doppler import (
    BlindDopplerAnalysisRequestV0_1,
    BlindDopplerBundleV0_1,
    BlindDopplerCandidateV0_1,
    SpectrogramRowV0_1,
    SpectrogramSliceV0_1,
)
from leo_flow.contracts.core import Digest, ReceiverChainId, SchemaRef, canonical_digest
from leo_flow.contracts.doppler_evidence import (
    AdvancedDopplerEvidenceBundleV0_1,
    AdvancedTrackEvidenceV0_1,
    BroadbandEvidenceV0_1,
    CandidatePathAssociationV0_1,
    CombEvidenceV0_1,
    DualReceiverEvidenceV0_1,
    PostBlindTleAssociationV0_1,
    SlopeBankEvidenceV0_1,
)
from leo_flow.contracts.waterfall import WaterfallAnalysisRequestV0_1
from leo_flow.contracts.waterfall_v0_2 import (
    V0_2,
    WaterfallAnalysisRequestV0_2,
    WaterfallAnalyzerV0_2,
    WaterfallBundleV0_2,
    WaterfallTileV0_2,
)
from leo_flow.storage.ports import RecordingView

from .waterfall_v0_2 import (
    WaterfallConfigV0_2,
    waterfall_algorithm_ref_v0_2,
    waterfall_config_ref_v0_2,
)

ADVANCED_DOPPLER_ALGORITHM_VERSION = "advanced-blind-doppler-v0.1"
DEFAULT_DOPPLER_RATES_HZ_S = tuple(
    sorted(
        {
            -100_000.0,
            -75_000.0,
            -50_000.0,
            -25_000.0,
            25_000.0,
            50_000.0,
            75_000.0,
            100_000.0,
            *(float(value) for value in range(-10_000, 10_001, 250)),
        }
    )
)


@dataclass(frozen=True)
class AdvancedDopplerConfigV0_1:
    doppler_rates_hz_s: tuple[float, ...] = DEFAULT_DOPPLER_RATES_HZ_S
    shuffle_offsets: tuple[int, ...] = (1, 3, 5, 7)
    maximum_viterbi_step_bins: int = 4
    viterbi_track_count: int = 3
    viterbi_motion_penalty: float = 0.25
    viterbi_peel_radius_bins: int = 1
    comb_spacing_bins: int | None = None
    wrong_comb_spacing_bins: int | None = None
    minimum_candidate_overlap_points: int = 4
    minimum_candidate_overlap_fraction: float = 0.5
    maximum_mean_candidate_distance_hz: float = 10_000.0
    maximum_candidate_point_distance_hz: float = 20_000.0

    def __post_init__(self) -> None:
        if not self.doppler_rates_hz_s or not all(
            math.isfinite(value) for value in self.doppler_rates_hz_s
        ):
            raise ValueError("advanced Doppler-rate bank must be nonempty and finite")
        if tuple(sorted(set(self.doppler_rates_hz_s))) != self.doppler_rates_hz_s:
            raise ValueError("advanced Doppler-rate bank must be unique and sorted")
        if not self.shuffle_offsets or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in self.shuffle_offsets
        ):
            raise ValueError("shuffle offsets must be positive integers")
        if len(set(self.shuffle_offsets)) != len(self.shuffle_offsets):
            raise ValueError("shuffle offsets must be unique")
        if self.maximum_viterbi_step_bins < 0:
            raise ValueError("maximum Viterbi step must be nonnegative")
        if not 1 <= self.viterbi_track_count <= 8:
            raise ValueError("Viterbi track count is outside its bound")
        if (
            not math.isfinite(self.viterbi_motion_penalty)
            or self.viterbi_motion_penalty < 0
        ):
            raise ValueError("Viterbi motion penalty must be finite and nonnegative")
        if self.viterbi_peel_radius_bins < 0:
            raise ValueError("Viterbi peel radius must be nonnegative")
        if self.comb_spacing_bins is None:
            if self.wrong_comb_spacing_bins is not None:
                raise ValueError("wrong comb spacing requires a selected spacing")
        elif self.comb_spacing_bins < 1:
            raise ValueError("comb spacing must be positive")
        if (
            self.wrong_comb_spacing_bins is not None
            and self.wrong_comb_spacing_bins < 1
        ):
            raise ValueError("wrong comb spacing must be positive")
        if self.minimum_candidate_overlap_points < 2:
            raise ValueError(
                "candidate association requires at least two overlap points"
            )
        for value, name in (
            (self.minimum_candidate_overlap_fraction, "minimum overlap fraction"),
            (self.maximum_mean_candidate_distance_hz, "maximum mean distance"),
            (self.maximum_candidate_point_distance_hz, "maximum point distance"),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.minimum_candidate_overlap_fraction > 1:
            raise ValueError("minimum overlap fraction cannot exceed one")


@dataclass(frozen=True)
class BroadbandMotionInputV0_1:
    candidate_path_digest: Digest
    lower_bins: tuple[float, ...]
    upper_bins: tuple[float, ...]
    texture_rows: tuple[tuple[float, ...], ...]
    maximum_texture_step_bins: int


@dataclass(frozen=True)
class DualReceiverPathInputV0_1:
    candidate_path_digest: Digest
    peer_candidate_path_digest: Digest
    peer_receiver_chain_id: ReceiverChainId
    own_bins: tuple[float, ...]
    peer_bins: tuple[float, ...]


@dataclass(frozen=True)
class PostBlindEphemerisInputV0_1:
    candidate_path_digest: Digest
    blind_candidate_rank: int
    predictions: tuple[tuple[str, tuple[float, ...]], ...]
    minimum_runner_up_margin_bins: float

    def __post_init__(self) -> None:
        if self.blind_candidate_rank < 1:
            raise ValueError("post-blind candidate rank must be positive")
        if not self.predictions:
            raise ValueError("post-blind ephemeris input requires predictions")
        names = tuple(name for name, _ in self.predictions)
        if tuple(sorted(set(names))) != names:
            raise ValueError("TLE prediction names must be unique and sorted")


@dataclass(frozen=True)
class PreparedTileDopplerV0_1:
    spectrogram: SpectrogramSliceV0_1
    basic: BlindDopplerBundleV0_1
    advanced: AdvancedDopplerEvidenceBundleV0_1


@dataclass(frozen=True)
class PreparedWaterfallDopplerV0_1:
    request: WaterfallAnalysisRequestV0_2
    waterfall: WaterfallBundleV0_2
    tiles: tuple[PreparedTileDopplerV0_1, ...]


def spectrogram_from_residual_v0_1(
    waterfall: WaterfallBundleV0_2, tile: WaterfallTileV0_2
) -> SpectrogramSliceV0_1:
    """Adapt exactly one residual layer without depending on analyzer internals."""

    if tile not in waterfall.tiles:
        raise ValueError("spectrogram tile is not part of the waterfall bundle")
    descriptor = {
        "producer_schema": waterfall.schema,
        "waterfall_product_id": str(waterfall.product_id),
        "waterfall_analysis_run_id": str(waterfall.analysis_run_id),
        "recording_id": str(waterfall.recording_id),
        "recording_digest": waterfall.input_recording_identity_digest,
        "segment_id": str(tile.segment_id),
        "receiver_chain_id": str(tile.receiver_chain_id),
        "layer": "temporal_median_residual_db",
        "center_frequency_hz": tile.center_frequency_hz,
        "frequency_bin_offsets_hz": tile.frequency_bin_offsets_hz,
        "rows": tuple(
            (row.midpoint_utc_ns, row.temporal_median_residual_db)
            for row in tile.time_bins
        ),
    }
    return SpectrogramSliceV0_1(
        schema=SchemaRef(SpectrogramSliceV0_1.SCHEMA_ID),
        input_identity_digest=canonical_digest(descriptor),
        segment_id=tile.segment_id,
        receiver_chain_id=tile.receiver_chain_id,
        center_frequency_hz=tile.center_frequency_hz,
        frequency_bin_offsets_hz=tile.frequency_bin_offsets_hz,
        power_reference=f"{tile.power_reference}-temporal-median-residual-db",
        rows=tuple(
            SpectrogramRowV0_1(row.midpoint_utc_ns, row.temporal_median_residual_db)
            for row in tile.time_bins
        ),
    )


class AdvancedBlindDopplerAnalyzerV0_1:
    """Derive controlled candidate evidence; no calibrated decision is emitted."""

    def __init__(self, config: AdvancedDopplerConfigV0_1 | None = None) -> None:
        self.config = config or AdvancedDopplerConfigV0_1()

    def analyze(
        self,
        spectrogram: SpectrogramSliceV0_1,
        basic: BlindDopplerBundleV0_1,
        *,
        broadband: BroadbandMotionInputV0_1 | None = None,
        dual_receiver: DualReceiverPathInputV0_1 | None = None,
        post_blind_ephemeris: PostBlindEphemerisInputV0_1 | None = None,
    ) -> AdvancedDopplerEvidenceBundleV0_1:
        if basic.input_identity_digest != spectrogram.input_identity_digest:
            raise ValueError("basic and advanced Doppler inputs differ")
        basic_digest = Digest.sha256(encode_blind_doppler_bundle(basic))
        auxiliary = tuple(
            sorted(
                (
                    canonical_digest(value)
                    for value in (broadband, dual_receiver, post_blind_ephemeris)
                    if value is not None
                ),
                key=str,
            )
        )
        if len(spectrogram.rows) < 6:
            return AdvancedDopplerEvidenceBundleV0_1(
                schema=SchemaRef(AdvancedDopplerEvidenceBundleV0_1.SCHEMA_ID),
                input_identity_digest=spectrogram.input_identity_digest,
                blind_bundle_digest=basic_digest,
                config_digest=canonical_digest(self.config),
                auxiliary_input_digests=auxiliary,
                algorithm_version=ADVANCED_DOPPLER_ALGORITHM_VERSION,
                candidate_only=True,
                spectral_peak_excess_reference=(
                    "temporal-median-residual-db-minus-per-row-median-db"
                ),
                association=None,
                slope_bank=None,
                peeled_tracks=(),
                warnings=(),
                reason_codes=("insufficient-rows-for-advanced-controls",),
            )

        matrix = tuple(row.power_db for row in spectrogram.rows)
        cadence_s = statistics.median(
            (later.midpoint_utc_ns - earlier.midpoint_utc_ns) / 1e9
            for earlier, later in zip(
                spectrogram.rows, spectrogram.rows[1:], strict=False
            )
        )
        bin_width_hz = statistics.median(
            later - earlier
            for earlier, later in zip(
                spectrogram.frequency_bin_offsets_hz,
                spectrogram.frequency_bin_offsets_hz[1:],
                strict=False,
            )
        )
        slopes = tuple(
            rate * cadence_s / bin_width_hz
            for rate in self.config.doppler_rates_hz_s
            if abs(rate * cadence_s / bin_width_hz) * (len(matrix) - 1)
            <= len(matrix[0]) - 1
        )
        slopes = tuple(sorted(set(slopes)))
        if not slopes:
            slopes = (0.0,)
        raw_bank = dedoppler_slope_bank(
            matrix, slopes, shuffle_offsets=self.config.shuffle_offsets
        )
        raw_tracks = viterbi_peel_tracks(
            matrix,
            maximum_step_bins=self.config.maximum_viterbi_step_bins,
            track_count=self.config.viterbi_track_count,
            motion_penalty=self.config.viterbi_motion_penalty,
            peel_radius_bins=self.config.viterbi_peel_radius_bins,
        )
        raw_path_digest = _advanced_path_digest(
            spectrogram, raw_bank.track.bins, "slope-bank"
        )
        association = _candidate_association(
            raw_bank.track.bins,
            raw_path_digest,
            spectrogram,
            basic,
            self.config,
        )
        primary_path_digest = association.candidate_path_digest
        bank = SlopeBankEvidenceV0_1(
            candidate_path_digest=primary_path_digest,
            source_input_digest=spectrogram.input_identity_digest,
            track=_track(
                raw_bank.track,
                cadence_s,
                bin_width_hz,
                raw_path_digest,
            ),
            basic_candidate_rank=association.basic_candidate_rank,
            heldout_score=raw_bank.heldout_score,
            stationary_score=raw_bank.stationary_score,
            opposite_slope_score=raw_bank.opposite_slope_score,
            time_shuffle_scores=raw_bank.shuffled_scores,
            training_rows=raw_bank.training_rows,
            validation_rows=raw_bank.validation_rows,
            test_rows=raw_bank.test_rows,
        )
        comb = None
        if self.config.comb_spacing_bins is not None:
            raw_comb = comb_support(
                matrix,
                raw_bank.track.bins,
                spacing_bins=self.config.comb_spacing_bins,
                wrong_spacing_bins=self.config.wrong_comb_spacing_bins,
            )
            comb = CombEvidenceV0_1(
                candidate_path_digest=primary_path_digest,
                source_input_digest=canonical_digest(
                    {
                        "spectrogram_digest": spectrogram.input_identity_digest,
                        "candidate_path_digest": primary_path_digest,
                        "spacing_bins": self.config.comb_spacing_bins,
                        "wrong_spacing_bins": self.config.wrong_comb_spacing_bins,
                    }
                ),
                spacing_bins=self.config.comb_spacing_bins,
                wrong_spacing_bins=(
                    self.config.wrong_comb_spacing_bins
                    if self.config.wrong_comb_spacing_bins is not None
                    else self.config.comb_spacing_bins + 1
                ),
                fit_score=raw_comb.fit_score,
                heldout_score=raw_comb.heldout_score,
                wrong_spacing_score=raw_comb.wrong_spacing_score,
            )
        broadband_result = _broadband(broadband, association)
        dual_result = _dual(dual_receiver, association)
        tle_result = _tle(post_blind_ephemeris, basic, association)
        warnings = []
        if broadband is not None and broadband_result is None:
            warnings.append("broadband-input-path-mismatch")
        if dual_receiver is not None and dual_result is None:
            warnings.append("dual-receiver-input-path-mismatch")
        if post_blind_ephemeris is not None and tle_result is None:
            warnings.append("ephemeris-input-path-mismatch")
        reasons = ("no-basic-blind-candidate",) if not basic.candidates else ()
        return AdvancedDopplerEvidenceBundleV0_1(
            schema=SchemaRef(AdvancedDopplerEvidenceBundleV0_1.SCHEMA_ID),
            input_identity_digest=spectrogram.input_identity_digest,
            blind_bundle_digest=basic_digest,
            config_digest=canonical_digest(self.config),
            auxiliary_input_digests=auxiliary,
            algorithm_version=ADVANCED_DOPPLER_ALGORITHM_VERSION,
            candidate_only=True,
            spectral_peak_excess_reference=(
                "temporal-median-residual-db-minus-per-row-median-db"
            ),
            association=association,
            slope_bank=bank,
            peeled_tracks=tuple(
                _track(
                    track,
                    cadence_s,
                    bin_width_hz,
                    _advanced_path_digest(spectrogram, track.bins, "viterbi"),
                )
                for track in raw_tracks
            ),
            comb=comb,
            broadband=broadband_result,
            dual_receiver=dual_result,
            tle_association=tle_result,
            warnings=tuple(sorted(warnings)),
            reason_codes=reasons,
        )


class WaterfallDopplerPipelineV0_1:
    """Compute v0.2 once, then analyze every exact segment/receiver tile."""

    def __init__(
        self,
        waterfall_analyzer: WaterfallAnalyzerV0_2,
        waterfall_config: WaterfallConfigV0_2,
        basic_config: BlindDopplerConfig | None = None,
        advanced_analyzer: AdvancedBlindDopplerAnalyzerV0_1 | None = None,
        *,
        maximum_candidates: int = 8,
    ) -> None:
        if not 0 < maximum_candidates <= 32:
            raise ValueError("maximum candidates is outside its bound")
        self._waterfall_analyzer = waterfall_analyzer
        self._waterfall_config = waterfall_config
        self._basic_config = basic_config or BlindDopplerConfig()
        self._basic_analyzer = BasicBlindDopplerAnalyzer(self._basic_config)
        self._advanced_analyzer = (
            advanced_analyzer or AdvancedBlindDopplerAnalyzerV0_1()
        )
        self._maximum_candidates = maximum_candidates

    def analyze(
        self,
        recording: RecordingView,
        legacy_request: WaterfallAnalysisRequestV0_1,
        *,
        broadband_inputs: Mapping[tuple[str, str], BroadbandMotionInputV0_1]
        | None = None,
        ephemeris_inputs: Mapping[tuple[str, str], PostBlindEphemerisInputV0_1]
        | None = None,
    ) -> PreparedWaterfallDopplerV0_1:
        request = WaterfallAnalysisRequestV0_2(
            schema=SchemaRef(WaterfallAnalysisRequestV0_2.SCHEMA_ID, V0_2),
            recording_id=legacy_request.recording_id,
            recording_object_ref=legacy_request.recording_object_ref,
            algorithm_ref=waterfall_algorithm_ref_v0_2(),
            config_ref=waterfall_config_ref_v0_2(self._waterfall_config),
            dependency_refs=legacy_request.dependency_refs,
            requested_output_schema=SchemaRef(WaterfallBundleV0_2.SCHEMA_ID, V0_2),
        )
        waterfall = self._waterfall_analyzer.analyze_waterfall(recording, request)
        spectrograms = tuple(
            spectrogram_from_residual_v0_1(waterfall, tile)
            for tile in waterfall.tiles
            if len(tile.time_bins) > 1
        )
        basics = tuple(self._basic(spectrogram) for spectrogram in spectrograms)
        initial = tuple(
            self._advanced_analyzer.analyze(spectrogram, basic)
            for spectrogram, basic in zip(spectrograms, basics, strict=True)
        )
        prepared: list[PreparedTileDopplerV0_1] = []
        for spectrogram, basic, first_advanced in zip(
            spectrograms, basics, initial, strict=True
        ):
            key = (str(spectrogram.segment_id), str(spectrogram.receiver_chain_id))
            peer = _peer_path(
                spectrogram,
                basic,
                first_advanced.association,
                spectrograms,
                basics,
            )
            prepared.append(
                PreparedTileDopplerV0_1(
                    spectrogram,
                    basic,
                    self._advanced_analyzer.analyze(
                        spectrogram,
                        basic,
                        broadband=None
                        if broadband_inputs is None
                        else broadband_inputs.get(key),
                        dual_receiver=peer,
                        post_blind_ephemeris=(
                            None
                            if ephemeris_inputs is None
                            else ephemeris_inputs.get(key)
                        ),
                    ),
                )
            )
        return PreparedWaterfallDopplerV0_1(request, waterfall, tuple(prepared))

    def _basic(self, spectrogram: SpectrogramSliceV0_1) -> BlindDopplerBundleV0_1:
        request = BlindDopplerAnalysisRequestV0_1(
            schema=SchemaRef(BlindDopplerAnalysisRequestV0_1.SCHEMA_ID),
            input_identity_digest=spectrogram.input_identity_digest,
            config_digest=blind_doppler_config_digest(self._basic_config),
            max_candidates=self._maximum_candidates,
        )
        return self._basic_analyzer.analyze_blind_doppler(spectrogram, request)


def _track(
    value: LinearTrack,
    cadence_s: float,
    bin_width_hz: float,
    path_digest: Digest,
) -> AdvancedTrackEvidenceV0_1:
    return AdvancedTrackEvidenceV0_1(
        path_digest=path_digest,
        bins=tuple(value.bins),
        slope_bins_per_row=value.slope_bins_per_row,
        drift_rate_hz_s=value.slope_bins_per_row * bin_width_hz / cadence_s,
        score=value.score,
        stationary_improvement=value.stationary_improvement,
    )


def _candidate_association(
    path: Sequence[int],
    path_digest: Digest,
    spectrogram: SpectrogramSliceV0_1,
    basic: BlindDopplerBundleV0_1,
    config: AdvancedDopplerConfigV0_1,
) -> CandidatePathAssociationV0_1:
    ranked: list[tuple[float, float, float, int, Digest]] = []
    bin_width_hz = statistics.median(
        later - earlier
        for earlier, later in zip(
            spectrogram.frequency_bin_offsets_hz,
            spectrogram.frequency_bin_offsets_hz[1:],
            strict=False,
        )
    )
    for candidate in basic.candidates:
        differences = [
            abs(point.interpolated_bin - path[point.row_index]) * bin_width_hz
            for point in candidate.points
            if point.row_index < len(path)
        ]
        overlap_fraction = len(differences) / max(len(path), len(candidate.points))
        if not differences:
            continue
        mean_distance = sum(differences) / len(differences)
        maximum_distance = max(differences)
        if (
            len(differences) >= config.minimum_candidate_overlap_points
            and overlap_fraction >= config.minimum_candidate_overlap_fraction
            and mean_distance <= config.maximum_mean_candidate_distance_hz
            and maximum_distance <= config.maximum_candidate_point_distance_hz
        ):
            ranked.append(
                (
                    mean_distance,
                    maximum_distance,
                    -overlap_fraction,
                    candidate.rank,
                    blind_candidate_path_digest(spectrogram, candidate),
                )
            )
    if not ranked:
        return CandidatePathAssociationV0_1(
            "advanced-path-only", path_digest, None, 0, 0.0, None, None
        )
    mean_distance, maximum_distance, negative_fraction, rank, digest = min(ranked)
    candidate = basic.candidates[rank - 1]
    return CandidatePathAssociationV0_1(
        "matched-basic-candidate",
        digest,
        rank,
        len(candidate.points),
        -negative_fraction,
        mean_distance,
        maximum_distance,
    )


def blind_candidate_path_digest(
    spectrogram: SpectrogramSliceV0_1, candidate: BlindDopplerCandidateV0_1
) -> Digest:
    return canonical_digest(
        {
            "spectrogram_digest": spectrogram.input_identity_digest,
            "path_kind": "basic-blind-candidate",
            "points": tuple(
                {
                    "row_index": point.row_index,
                    "midpoint_utc_ns": point.midpoint_utc_ns,
                    "interpolated_bin": point.interpolated_bin,
                    "frequency_hz": point.frequency_hz,
                }
                for point in candidate.points
            ),
        }
    )


def _advanced_path_digest(
    spectrogram: SpectrogramSliceV0_1, path: Sequence[int], path_kind: str
) -> Digest:
    return canonical_digest(
        {
            "spectrogram_digest": spectrogram.input_identity_digest,
            "path_kind": path_kind,
            "bins": tuple(path),
        }
    )


def _broadband(
    value: BroadbandMotionInputV0_1 | None,
    association: CandidatePathAssociationV0_1,
) -> BroadbandEvidenceV0_1 | None:
    if (
        value is None
        or value.candidate_path_digest != association.candidate_path_digest
    ):
        return None
    result = broadband_motion_support(
        value.lower_bins,
        value.upper_bins,
        value.texture_rows,
        maximum_texture_step_bins=value.maximum_texture_step_bins,
    )
    return BroadbandEvidenceV0_1(
        value.candidate_path_digest, canonical_digest(value), **result.__dict__
    )


def _dual(
    value: DualReceiverPathInputV0_1 | None,
    association: CandidatePathAssociationV0_1,
) -> DualReceiverEvidenceV0_1 | None:
    if (
        value is None
        or value.candidate_path_digest != association.candidate_path_digest
    ):
        return None
    result = dual_receiver_consensus(value.own_bins, value.peer_bins)
    return DualReceiverEvidenceV0_1(
        candidate_path_digest=value.candidate_path_digest,
        peer_candidate_path_digest=value.peer_candidate_path_digest,
        source_input_digest=canonical_digest(value),
        peer_receiver_chain_id=value.peer_receiver_chain_id,
        common_slope_bins_per_row=result.common_slope_bins_per_row,
        slope_difference=result.slope_difference,
        receiver_offsets_bins=result.receiver_offsets_bins,
        offset_removed_rms_bins=result.offset_removed_rms_bins,
        path_correlation=result.path_correlation,
    )


def _tle(
    value: PostBlindEphemerisInputV0_1 | None,
    basic: BlindDopplerBundleV0_1,
    association: CandidatePathAssociationV0_1,
) -> PostBlindTleAssociationV0_1 | None:
    if (
        value is None
        or value.candidate_path_digest != association.candidate_path_digest
    ):
        return None
    candidate = next(
        (item for item in basic.candidates if item.rank == value.blind_candidate_rank),
        None,
    )
    if candidate is None:
        raise ValueError("post-blind ephemeris input selects an absent candidate")
    by_row = {point.row_index: point.interpolated_bin for point in candidate.points}
    if tuple(sorted(by_row)) != tuple(range(len(by_row))):
        raise ValueError("TLE association requires a contiguous blind candidate path")
    observed = tuple(by_row[index] for index in range(len(by_row)))
    result = associate_tle_post_blind(
        observed,
        dict(value.predictions),
        blind_qualified=True,
        minimum_runner_up_margin_bins=value.minimum_runner_up_margin_bins,
    )
    return PostBlindTleAssociationV0_1(
        value.candidate_path_digest, canonical_digest(value), **result.__dict__
    )


def _peer_path(
    spectrogram: SpectrogramSliceV0_1,
    basic: BlindDopplerBundleV0_1,
    association: CandidatePathAssociationV0_1 | None,
    spectrograms: Sequence[SpectrogramSliceV0_1],
    basics: Sequence[BlindDopplerBundleV0_1],
) -> DualReceiverPathInputV0_1 | None:
    if association is None or association.basic_candidate_rank is None:
        return None
    own_candidate = next(
        (
            candidate
            for candidate in basic.candidates
            if candidate.rank == association.basic_candidate_rank
        ),
        None,
    )
    if own_candidate is None:
        return None
    own = {point.row_index: point.interpolated_bin for point in own_candidate.points}
    own_rate = next(
        fit.drift_rate_hz_s
        for fit in own_candidate.fits
        if fit.order == own_candidate.selected_order
    )
    choices: list[
        tuple[
            float,
            int,
            int,
            SpectrogramSliceV0_1,
            BlindDopplerCandidateV0_1,
            tuple[int, ...],
        ]
    ] = []
    for candidate_spectrogram, candidate_basic in zip(
        spectrograms, basics, strict=True
    ):
        if (
            candidate_spectrogram.segment_id != spectrogram.segment_id
            or candidate_spectrogram.receiver_chain_id == spectrogram.receiver_chain_id
            or not candidate_basic.candidates
        ):
            continue
        for peer_candidate in candidate_basic.candidates:
            peer = {
                point.row_index: point.interpolated_bin
                for point in peer_candidate.points
            }
            common = tuple(sorted(set(own) & set(peer)))
            if len(common) < 4:
                continue
            peer_rate = next(
                fit.drift_rate_hz_s
                for fit in peer_candidate.fits
                if fit.order == peer_candidate.selected_order
            )
            choices.append(
                (
                    abs(peer_rate - own_rate),
                    -len(common),
                    peer_candidate.rank,
                    candidate_spectrogram,
                    peer_candidate,
                    common,
                )
            )
    if not choices:
        return None
    _, _, _, peer_spectrogram, peer_candidate, common = min(
        choices, key=lambda item: item[:3]
    )
    peer = {point.row_index: point.interpolated_bin for point in peer_candidate.points}
    return DualReceiverPathInputV0_1(
        association.candidate_path_digest,
        blind_candidate_path_digest(peer_spectrogram, peer_candidate),
        peer_spectrogram.receiver_chain_id,
        tuple(own[index] for index in common),
        tuple(peer[index] for index in common),
    )
