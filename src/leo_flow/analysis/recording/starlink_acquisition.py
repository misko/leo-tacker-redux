"""Bounded, receiver-aware v0.3 edge-pilot acquisition.

This is native Redux code.  The historical ``leo-tracker`` implementation is
an offline numerical oracle only and is never imported here.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from leo_flow.contracts._validation import require_finite, require_token
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    Provenance,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    canonical_digest,
)
from leo_flow.contracts.starlink_acquisition import (
    V0_3,
    StarlinkAcquisitionBundleV0_3,
    StarlinkAcquisitionCandidateV0_3,
)

from .api import AnalysisExecutionContext
from .starlink import FRAME_RATE_HZ, KnownCodePilotTemplatePairV0_1
from .starlink_templates import OFDM_SYMBOL_DURATION_S

ALGORITHM_ID = "starlink-edge-pilot-multibasin-acquisition"
ALGORITHM_VERSION = "0.3.0"
CONFIG_SCHEMA_ID = "org.leo-flow.starlink-edge-pilot-acquisition-config"
MINIMUM_CFO_COVERAGE_HZ = 400_000.0
# The prefilter is part of ACQUIRE.  VERIFY remains genuinely held out until
# every retained basin has completed epoch/CFO refinement.
DEFAULT_ANCHOR_SYMBOLS = tuple(range(2, 302, 26))
DEFAULT_ACQUIRE_SYMBOLS = tuple(range(2, 302, 2))
DEFAULT_VERIFY_SYMBOLS = tuple(range(3, 302, 2))


@dataclass(frozen=True)
class StarlinkAcquisitionConfigV0_3:
    """Physical CFO profile, candidate policy, and hard resource ceilings."""

    receiver_cfo_profile_id: str
    cfo_min_hz: float = -400_000.0
    cfo_max_hz: float = 400_000.0
    coarse_cfo_step_hz: float = 80_000.0
    fine_cfo_radius_hz: float = 80_000.0
    fine_cfo_step_hz: float = 500.0
    retained_candidate_count: int = 8
    candidate_epoch_separation_samples: int = 20
    candidate_cfo_separation_hz: float = 80_000.0
    epoch_refinement_radius_samples: int = 1
    anchor_symbols: tuple[int, ...] = DEFAULT_ANCHOR_SYMBOLS
    acquire_symbols: tuple[int, ...] = DEFAULT_ACQUIRE_SYMBOLS
    verify_symbols: tuple[int, ...] = DEFAULT_VERIFY_SYMBOLS
    minimum_frame_support: int = 2
    maximum_probe_samples: int = 50_000
    maximum_coarse_search_cells: int = 100_000
    maximum_refinement_search_cells: int = 100_000
    maximum_working_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        require_token(self.receiver_cfo_profile_id, "receiver_cfo_profile_id")
        for name in (
            "cfo_min_hz",
            "cfo_max_hz",
            "coarse_cfo_step_hz",
            "fine_cfo_radius_hz",
            "fine_cfo_step_hz",
            "candidate_cfo_separation_hz",
        ):
            require_finite(getattr(self, name), name)
        if (
            self.cfo_min_hz > -MINIMUM_CFO_COVERAGE_HZ
            or self.cfo_max_hz < MINIMUM_CFO_COVERAGE_HZ
        ):
            raise ValueError("v0.3 CFO profile must cover at least -400 to +400 kHz")
        if self.cfo_min_hz >= self.cfo_max_hz:
            raise ValueError("CFO domain must be non-empty")
        if (
            min(
                self.coarse_cfo_step_hz,
                self.fine_cfo_radius_hz,
                self.fine_cfo_step_hz,
                self.candidate_cfo_separation_hz,
            )
            <= 0
        ):
            raise ValueError("CFO steps, radius, and separation must be positive")
        for name in (
            "retained_candidate_count",
            "candidate_epoch_separation_samples",
            "epoch_refinement_radius_samples",
            "minimum_frame_support",
            "maximum_probe_samples",
            "maximum_coarse_search_cells",
            "maximum_refinement_search_cells",
            "maximum_working_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        _symbols(self.anchor_symbols, "anchor_symbols")
        _symbols(self.acquire_symbols, "acquire_symbols")
        _symbols(self.verify_symbols, "verify_symbols")
        if set(self.acquire_symbols) & set(self.verify_symbols):
            raise ValueError("acquire and verify pilot symbols must be disjoint")
        if not set(self.anchor_symbols) <= set(self.acquire_symbols):
            raise ValueError("coarse anchor symbols must be a subset of acquire")
        if tuple(sorted(self.acquire_symbols + self.verify_symbols)) != tuple(
            range(2, 302)
        ):
            raise ValueError("acquire and verify must partition all 300 pilots")

    @property
    def coarse_cfo_hypotheses_hz(self) -> tuple[float, ...]:
        return _bounded_grid(self.cfo_min_hz, self.cfo_max_hz, self.coarse_cfo_step_hz)


class StarlinkAcquisitionV0_3:
    """Retain coarse basins, refine each, then adjudicate on held-out pilots."""

    def __init__(
        self,
        config: StarlinkAcquisitionConfigV0_3,
        execution: AnalysisExecutionContext,
    ) -> None:
        self._config = config
        self._execution = execution

    def analyze_receiver(
        self,
        samples: Sequence[complex],
        *,
        recording_id: RecordingId,
        recording_identity_digest: Digest,
        segment_id: SegmentId,
        receiver_chain_id: ReceiverChainId,
        templates: KnownCodePilotTemplatePairV0_1,
    ) -> StarlinkAcquisitionBundleV0_3:
        values = np.asarray(samples, dtype=np.complex128)
        self._validate_inputs(values, templates)
        epoch_count = round(templates.sample_rate_hz / FRAME_RATE_HZ)
        coarse_cfos = self._config.coarse_cfo_hypotheses_hz
        coarse_cells = epoch_count * len(coarse_cfos)
        if coarse_cells > self._config.maximum_coarse_search_cells:
            raise ValueError("declared coarse search exceeds its cell bound")

        score_maps: dict[float, np.ndarray] = {}
        peaks: list[tuple[float, int, float]] = []
        exact = np.asarray(templates.exact_samples, dtype=np.complex128)
        for cfo in coarse_cfos:
            scores = _folded_anchor_scores(
                values,
                exact,
                templates.sample_rate_hz,
                cfo,
                self._config.anchor_symbols,
                epoch_count,
            )
            score_maps[cfo] = scores
            for epoch in _local_peak_indexes(scores):
                peaks.append((float(scores[epoch]), epoch, cfo))
        peaks.sort(key=lambda item: (item[0], -abs(item[2]), -item[1]), reverse=True)
        retained = _retain_separated(
            peaks,
            self._config.retained_candidate_count,
            self._config.candidate_epoch_separation_samples,
            self._config.candidate_cfo_separation_hz,
            epoch_count,
        )
        if not retained:
            raise ValueError("coarse acquisition produced no supported candidates")

        fine_grid_counts = [
            len(self._fine_grid(coarse_cfo)) for _, _, coarse_cfo in retained
        ]
        # One acquire rescore at the interpolated CFO and held-out exact/control
        # scores are also hypothesis evaluations and are counted explicitly.
        refinement_cells = sum(count + 3 for count in fine_grid_counts)
        if refinement_cells > self._config.maximum_refinement_search_cells:
            raise ValueError("declared refinement exceeds its cell bound")

        candidates = []
        for coarse_score, coarse_epoch, coarse_cfo in retained:
            refined_epoch = self._refine_epoch(
                score_maps[coarse_cfo], coarse_epoch, epoch_count
            )
            fine_grid = self._fine_grid(coarse_cfo)
            fine_scores = tuple(
                _normalized_frame_score(
                    values,
                    exact,
                    templates.sample_rate_hz,
                    refined_epoch,
                    cfo,
                    self._config.acquire_symbols,
                )[0]
                for cfo in fine_grid
            )
            best_index = max(
                range(len(fine_grid)),
                key=lambda index: (
                    fine_scores[index],
                    -abs(fine_grid[index]),
                    -fine_grid[index],
                ),
            )
            refined_cfo = _quadratic_peak(fine_grid, fine_scores, best_index)
            acquire_score, _ = _normalized_frame_score(
                values,
                exact,
                templates.sample_rate_hz,
                refined_epoch,
                refined_cfo,
                self._config.acquire_symbols,
            )
            verify_score, support = _normalized_frame_score(
                values,
                exact,
                templates.sample_rate_hz,
                refined_epoch,
                refined_cfo,
                self._config.verify_symbols,
            )
            control_score, control_support = _normalized_frame_score(
                values,
                np.asarray(templates.conditioned_control_samples, np.complex128),
                templates.sample_rate_hz,
                refined_epoch,
                refined_cfo,
                self._config.verify_symbols,
            )
            if min(support, control_support) < self._config.minimum_frame_support:
                continue
            candidates.append(
                StarlinkAcquisitionCandidateV0_3(
                    coarse_epoch,
                    coarse_cfo,
                    coarse_score,
                    refined_epoch,
                    refined_cfo,
                    acquire_score,
                    verify_score,
                    control_score,
                    verify_score - control_score,
                    min(support, control_support),
                    0,
                )
            )
        if not candidates:
            raise ValueError("no refined candidate has minimum frame support")
        ordered = sorted(
            candidates,
            key=lambda item: (
                item.verify_minus_control_margin,
                item.verify_score,
                item.acquire_score,
                -abs(item.refined_cfo_hz),
                -item.refined_epoch_sample,
            ),
            reverse=True,
        )
        ranked = tuple(
            StarlinkAcquisitionCandidateV0_3(
                item.coarse_epoch_sample,
                item.coarse_cfo_hz,
                item.coarse_score,
                item.refined_epoch_sample,
                item.refined_cfo_hz,
                item.acquire_score,
                item.verify_score,
                item.conditioned_control_score,
                item.verify_minus_control_margin,
                item.frame_support,
                rank,
            )
            for rank, item in enumerate(ordered)
        )

        algorithm_ref = starlink_acquisition_algorithm_ref_v0_3()
        config_ref = starlink_acquisition_config_ref_v0_3(self._config)
        search_identity = canonical_digest(
            {
                "algorithm_digest": str(algorithm_ref.digest),
                "config_digest": str(config_ref.digest),
                "recording_identity_digest": str(recording_identity_digest),
                "segment_id": str(segment_id),
                "receiver_chain_id": str(receiver_chain_id),
                "exact_template_digest": str(templates.exact_ref.digest),
                "control_template_digest": str(
                    templates.conditioned_control_ref.digest
                ),
                "probe_sample_count": len(values),
                "coarse_search_cell_count": coarse_cells,
                "refinement_search_cell_count": refinement_cells,
            }
        )
        input_digest = canonical_digest(
            {
                "recording_identity_digest": str(recording_identity_digest),
                "segment_id": str(segment_id),
                "receiver_chain_id": str(receiver_chain_id),
            }
        )
        provenance = Provenance(
            self._execution.producer_name,
            self._execution.producer_version,
            self._execution.git_commit,
            self._execution.environment_digest,
            config_ref.digest,
            (input_digest,),
            (
                algorithm_ref.digest,
                templates.exact_ref.digest,
                templates.conditioned_control_ref.digest,
            ),
            self._execution.started_utc_ns,
            self._execution.completed_utc_ns,
            self._execution.host_class,
        )
        token = canonical_digest(
            {"search_identity_digest": str(search_identity), "candidates": ranked}
        ).value
        return StarlinkAcquisitionBundleV0_3(
            SchemaRef(StarlinkAcquisitionBundleV0_3.SCHEMA_ID, V0_3),
            f"slacq_{token[:32]}",
            recording_id,
            recording_identity_digest,
            segment_id,
            receiver_chain_id,
            self._config.receiver_cfo_profile_id,
            templates.edge,
            templates.sample_rate_hz,
            len(values),
            algorithm_ref,
            config_ref,
            templates.exact_ref,
            templates.conditioned_control_ref,
            search_identity,
            self._config.cfo_min_hz,
            self._config.cfo_max_hz,
            coarse_cells,
            refinement_cells,
            len(peaks),
            ranked,
            0,
            self._config.acquire_symbols,
            self._config.verify_symbols,
            provenance,
            True,
            (
                "held-out-pilot-adjudication",
                "whole-revised-search-calibration-required",
                "known-published-pilot-not-user-payload",
                "per-receiver-evidence-no-cross-radio-phase-combination",
            ),
        )

    def _validate_inputs(
        self,
        values: np.ndarray,
        templates: KnownCodePilotTemplatePairV0_1,
    ) -> None:
        if values.ndim != 1 or values.size <= 0:
            raise ValueError("acquisition requires a non-empty one-dimensional stream")
        if values.size > self._config.maximum_probe_samples:
            raise ValueError("acquisition probe exceeds maximum_probe_samples")
        if not np.all(np.isfinite(values)):
            raise ValueError("acquisition samples must be finite")
        frame_content = round(302 * templates.sample_rate_hz * OFDM_SYMBOL_DURATION_S)
        if values.size < frame_content:
            raise ValueError("acquisition probe contains no complete pilot frame")
        estimated_working_bytes = int(values.size) * (
            np.dtype(np.complex128).itemsize * 4 + np.dtype(np.float64).itemsize * 6
        )
        if estimated_working_bytes > self._config.maximum_working_bytes:
            raise ValueError("declared acquisition exceeds maximum_working_bytes")

    def _refine_epoch(
        self, scores: np.ndarray, coarse_epoch: int, epoch_count: int
    ) -> int:
        radius = self._config.epoch_refinement_radius_samples
        choices = tuple(
            range(
                max(0, coarse_epoch - radius),
                min(epoch_count - 1, coarse_epoch + radius) + 1,
            )
        )
        return max(choices, key=lambda epoch: (scores[epoch], -epoch))

    def _fine_grid(self, coarse_cfo: float) -> tuple[float, ...]:
        return _bounded_grid(
            max(self._config.cfo_min_hz, coarse_cfo - self._config.fine_cfo_radius_hz),
            min(self._config.cfo_max_hz, coarse_cfo + self._config.fine_cfo_radius_hz),
            self._config.fine_cfo_step_hz,
        )


def starlink_acquisition_algorithm_ref_v0_3() -> ArtifactRef:
    return ArtifactRef(
        "starlink-edge-pilot-multibasin-acquisition-v0.3",
        canonical_digest(
            {
                "algorithm_id": ALGORITHM_ID,
                "algorithm_version": ALGORITHM_VERSION,
                "coarse_statistic": "sample-level-folded-symbol-normalized-match",
                "coarse_candidates": "separated-multibasin-retention",
                "refinement": "sample-level-epoch-and-fine-cfo-parabolic",
                "selection": "disjoint-held-out-pilot-exact-minus-roll17-control",
                "inter_frame_combination": "noncoherent",
                "decision": "none-without-whole-revised-search-calibration",
            }
        ),
        SchemaRef("org.leo-flow.recording-algorithm", V0_3),
    )


def starlink_acquisition_config_ref_v0_3(
    config: StarlinkAcquisitionConfigV0_3,
) -> ArtifactRef:
    return ArtifactRef(
        "starlink-edge-pilot-acquisition-config-v0.3",
        canonical_digest(config),
        SchemaRef(CONFIG_SCHEMA_ID, V0_3),
    )


def _folded_anchor_scores(
    values: np.ndarray,
    template: np.ndarray,
    sample_rate_hz: float,
    cfo_hz: float,
    symbols: tuple[int, ...],
    epoch_count: int,
) -> np.ndarray:
    indexes = np.arange(values.size, dtype=np.float64)
    derotated = values * np.exp(-2j * np.pi * cfo_hz * indexes / sample_rate_hz)
    scores = np.zeros(epoch_count, dtype=np.float64)
    support = np.zeros(epoch_count, dtype=np.int32)
    period = sample_rate_hz / FRAME_RATE_HZ
    for symbol in symbols:
        local_start = round(symbol * sample_rate_hz * OFDM_SYMBOL_DURATION_S)
        local_stop = round((symbol + 1) * sample_rate_hz * OFDM_SYMBOL_DURATION_S)
        reference = template[local_start:local_stop]
        correlation = np.convolve(derotated, np.conj(reference[::-1]), mode="valid")
        energy = np.convolve(
            np.abs(derotated) ** 2,
            np.ones(reference.size, dtype=np.float64),
            mode="valid",
        )
        denominator = np.sqrt(float(np.vdot(reference, reference).real) * energy)
        normalized = np.divide(
            np.abs(correlation),
            denominator,
            out=np.zeros_like(denominator),
            where=denominator > 0,
        )
        frame = 0
        while True:
            starts = np.arange(epoch_count) + local_start + round(frame * period)
            valid = starts < normalized.size
            if not np.any(valid):
                break
            scores[valid] += normalized[starts[valid]]
            support[valid] += 1
            frame += 1
    return np.divide(
        scores,
        support,
        out=np.zeros_like(scores),
        where=support > 0,
    )


def normalized_frame_score_v0_3(
    values: np.ndarray,
    template: np.ndarray,
    sample_rate_hz: float,
    epoch_sample: int,
    cfo_hz: float,
    symbols: tuple[int, ...],
) -> tuple[float, int]:
    sample_indexes = _pilot_sample_indexes(sample_rate_hz, symbols)
    references = template[sample_indexes]
    template_energy = float(np.vdot(references, references).real)
    rotation = np.exp(-2j * np.pi * cfo_hz * sample_indexes / sample_rate_hz)
    period = sample_rate_hz / FRAME_RATE_HZ
    per_frame = []
    frame = 0
    while True:
        start = epoch_sample + round(frame * period)
        absolute = start + sample_indexes
        if absolute[-1] >= values.size:
            break
        received = values[absolute]
        data_energy = float(np.vdot(received, received).real)
        denominator = math.sqrt(template_energy * data_energy)
        per_frame.append(
            float(abs(np.vdot(references, received * rotation)) / denominator)
            if denominator
            else 0.0
        )
        frame += 1
    return (float(np.mean(per_frame)) if per_frame else 0.0, len(per_frame))


# Keep the implementation-private name for the immutable v0.3 acquisition path.
# Additive consumers use ``normalized_frame_score_v0_3`` explicitly.
_normalized_frame_score = normalized_frame_score_v0_3


def _pilot_sample_indexes(
    sample_rate_hz: float, symbols: tuple[int, ...]
) -> np.ndarray:
    return np.concatenate(
        tuple(
            np.arange(
                round(symbol * sample_rate_hz * OFDM_SYMBOL_DURATION_S),
                round((symbol + 1) * sample_rate_hz * OFDM_SYMBOL_DURATION_S),
            )
            for symbol in symbols
        )
    )


def _local_peak_indexes(scores: np.ndarray) -> tuple[int, ...]:
    if scores.size == 1:
        return (0,)
    result = []
    for index, score in enumerate(scores):
        left = scores[index - 1] if index else -math.inf
        right = scores[index + 1] if index + 1 < scores.size else -math.inf
        if score >= left and score >= right and (score > left or score > right):
            result.append(index)
    return tuple(result)


def _retain_separated(
    peaks: list[tuple[float, int, float]],
    count: int,
    epoch_separation: int,
    cfo_separation: float,
    epoch_count: int,
) -> tuple[tuple[float, int, float], ...]:
    retained: list[tuple[float, int, float]] = []
    for candidate in peaks:
        _, epoch, cfo = candidate
        separated = True
        for _, other_epoch, other_cfo in retained:
            epoch_distance = abs(epoch - other_epoch)
            epoch_distance = min(epoch_distance, epoch_count - epoch_distance)
            if (
                epoch_distance < epoch_separation
                and abs(cfo - other_cfo) <= cfo_separation
            ):
                separated = False
                break
        if separated:
            retained.append(candidate)
            if len(retained) == count:
                break
    return tuple(retained)


def _quadratic_peak(
    grid: tuple[float, ...], scores: tuple[float, ...], index: int
) -> float:
    if index == 0 or index + 1 == len(grid):
        return grid[index]
    left, center, right = scores[index - 1 : index + 2]
    curvature = left - 2 * center + right
    if not math.isfinite(curvature) or curvature >= -1e-15:
        return grid[index]
    step = grid[index + 1] - grid[index]
    offset = 0.5 * (left - right) / curvature * step
    offset = min(step, max(-step, offset))
    return float(grid[index] + offset)


def _bounded_grid(start: float, stop: float, step: float) -> tuple[float, ...]:
    count = math.floor((stop - start) / step + 1e-12)
    values = [float(start + index * step) for index in range(count + 1)]
    if not math.isclose(values[-1], stop, rel_tol=0, abs_tol=1e-9):
        values.append(float(stop))
    return tuple(values)


def _symbols(values: tuple[int, ...], name: str) -> None:
    if (
        not values
        or tuple(sorted(set(values))) != values
        or any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        )
        or values[0] < 2
        or values[-1] > 301
    ):
        raise ValueError(f"{name} must be a sorted unique subset of 2..301")
