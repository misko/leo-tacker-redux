"""Native fixed-cadence parity replay for legacy ``pilot_symbolwise_v3``.

The historical repository is a numerical oracle only.  This module contains a
dependency-free NumPy implementation and never imports ``leo-tracker``.
"""

from __future__ import annotations

import cmath
import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

import numpy as np

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
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_surrogate_null import StarlinkSearchPatternRole
from leo_flow.contracts.starlink_symbolwise_replay import (
    MAXIMUM_REPLAY_PATTERNS,
    MAXIMUM_REPLAY_WINDOWS,
    V0_1,
    StarlinkReceiverFrequencyCenterV0_1,
    StarlinkSymbolwisePatternEvidenceV0_1,
    StarlinkSymbolwiseReplayBundleV0_1,
    StarlinkSymbolwiseWindowEvidenceV0_1,
)

from .api import AnalysisExecutionContext
from .starlink import FRAME_RATE_HZ, template_samples_digest
from .starlink_surrogate_null import (
    StarlinkPatternTemplateV0_1,
    precommitted_surrogate_codebook_v0_1,
    precommitted_surrogate_states_v0_1,
    qin_exact_search_pattern_v0_1,
)
from .starlink_templates import (
    CYCLIC_PREFIX_DURATION_S,
    OFDM_SYMBOL_DURATION_S,
    SUBCARRIER_SPACING_HZ,
    qin_edge_pilot_indices_v1,
    qin_edge_pilot_template_pair_v0_1,
)

ALGORITHM_ID = "starlink-pilot-symbolwise-legacy-parity-replay"
ALGORITHM_VERSION = "0.1.0"
CONFIG_SCHEMA_ID = "org.leo-flow.starlink-symbolwise-replay-config"
LEGACY_ORACLE_COMMIT = "0bb80d14759fd8496b74e7d3219a690be18565a6"
WINDOW_DURATION_S = 0.010
CADENCE_S = 0.100
TIMING_SEARCH_SPAN_HZ = 320_000.0
TIMING_SEARCH_STEP_LIMIT_HZ = 100_000.0
SYMBOLWISE_CFO_LIMIT_HZ = 350_000.0
SYMBOLWISE_CFO_RADIUS_HZ = 100_000.0
CONDITIONED_CFO_RADIUS_HZ = 2_000.0
CONDITIONED_CFO_STEP_HZ = 100.0
ANCHOR_SYMBOL_COUNT = 24
RETAINED_CANDIDATE_COUNT = 4
CANDIDATE_SEPARATION_SAMPLES = 20
CONTROL_SYMBOL_ROLL = 17


class StarlinkSymbolwiseWindowReaderV0_1(Protocol):
    """Narrow random-access port; implementations retain storage ownership."""

    def read_window(
        self, start_sample: int, sample_count: int
    ) -> Sequence[complex]: ...


@dataclass(frozen=True)
class StarlinkSymbolwiseReplayConfigV0_1:
    """Fixed science plus explicit hard ceilings for one receiver replay."""

    surrogate_count: int = 4
    maximum_windows: int = MAXIMUM_REPLAY_WINDOWS
    maximum_window_samples: int = 50_000
    maximum_timing_search_cells: int = 100_000_000
    maximum_refinement_search_cells: int = 1_000_000
    maximum_working_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if (
            isinstance(self.surrogate_count, bool)
            or not isinstance(self.surrogate_count, int)
            or not 1 <= self.surrogate_count < MAXIMUM_REPLAY_PATTERNS
        ):
            raise ValueError("surrogate_count must lie in [1,4]")
        for name in (
            "maximum_windows",
            "maximum_window_samples",
            "maximum_timing_search_cells",
            "maximum_refinement_search_cells",
            "maximum_working_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.maximum_windows > MAXIMUM_REPLAY_WINDOWS:
            raise ValueError("maximum_windows exceeds the v0.1 contract bound")


@dataclass(frozen=True)
class StarlinkSymbolwiseReplayResourcePlanV0_1:
    """Deterministic work and coverage accounting before any IQ read."""

    window_sample_count: int
    cadence_sample_count: int
    window_start_samples: tuple[int, ...]
    analyzed_union_sample_count: int
    coverage_fraction: float
    pattern_count: int
    timing_search_cell_count: int
    refinement_search_cell_count: int
    estimated_maximum_working_bytes: int


@dataclass(frozen=True)
class _TimingCandidate:
    epoch_sample: int
    frequency_offset_hz: float
    folded_score: float
    folded_median: float
    peak_to_median: float
    symbol_frame_support: int


@dataclass(frozen=True)
class _ConditionedScore:
    score: float
    maximum_score: float
    frame_support: int
    frequency_offset_hz: float


@dataclass(frozen=True)
class _SymbolwiseTrack:
    coarse_cfo_hz: float
    residual_cfo_hz: float
    refined_cfo_hz: float
    target_score: float
    control_score: float
    coherence: float
    control_coherence: float
    symbol_match_count: int


class StarlinkSymbolwiseReplayAnalyzerV0_1:
    """Replay every fixed 10 ms/100 ms window with symmetric patterns."""

    def __init__(
        self,
        config: StarlinkSymbolwiseReplayConfigV0_1,
        execution: AnalysisExecutionContext,
    ) -> None:
        self._config = config
        self._execution = execution

    def analyze_receiver(
        self,
        reader: StarlinkSymbolwiseWindowReaderV0_1,
        *,
        recording_id: RecordingId,
        recording_identity_digest: Digest,
        segment_id: SegmentId,
        receiver_chain_id: ReceiverChainId,
        edge: StarlinkEdge,
        sample_rate_hz: float,
        segment_sample_count: int,
        frequency_center: StarlinkReceiverFrequencyCenterV0_1,
    ) -> StarlinkSymbolwiseReplayBundleV0_1:
        plan = self.resource_plan(
            sample_rate_hz=sample_rate_hz,
            segment_sample_count=segment_sample_count,
            frequency_center=frequency_center,
        )
        window_samples = plan.window_sample_count
        cadence_samples = plan.cadence_sample_count
        starts = plan.window_start_samples
        patterns = self._patterns(sample_rate_hz, edge)
        control_templates = self._selection_controls(patterns, sample_rate_hz, edge)

        windows = []
        for window_index, start in enumerate(starts):
            values = np.asarray(reader.read_window(start, window_samples), np.complex64)
            if values.ndim != 1 or values.size != window_samples:
                raise ValueError("symbolwise reader returned another window geometry")
            if not np.all(np.isfinite(values)):
                raise ValueError("symbolwise reader returned non-finite samples")
            pattern_evidence = tuple(
                _analyze_pattern(
                    values,
                    sample_rate_hz,
                    frequency_center.center_cfo_hz,
                    pattern,
                    control,
                )
                for pattern, control in zip(patterns, control_templates, strict=True)
            )
            windows.append(
                StarlinkSymbolwiseWindowEvidenceV0_1(
                    window_index,
                    start,
                    start + window_samples,
                    _window_digest(values),
                    pattern_evidence,
                )
            )

        algorithm_ref = starlink_symbolwise_replay_algorithm_ref_v0_1()
        config_ref = starlink_symbolwise_replay_config_ref_v0_1(self._config)
        codebook_digest = canonical_digest(tuple(item.identity for item in patterns))
        input_digest = canonical_digest(
            {
                "recording_identity_digest": str(recording_identity_digest),
                "segment_id": str(segment_id),
                "receiver_chain_id": str(receiver_chain_id),
                "window_input_digests": tuple(
                    str(item.input_digest) for item in windows
                ),
            }
        )
        dependencies = tuple(
            dict.fromkeys(
                (
                    algorithm_ref.digest,
                    frequency_center.digest,
                    frequency_center.source_ref.digest,
                    *(item.identity.template_ref.digest for item in patterns),
                    *(item.digest for item in control_templates),
                )
            )
        )
        provenance = Provenance(
            self._execution.producer_name,
            self._execution.producer_version,
            self._execution.git_commit,
            self._execution.environment_digest,
            config_ref.digest,
            (input_digest,),
            dependencies,
            self._execution.started_utc_ns,
            self._execution.completed_utc_ns,
            self._execution.host_class,
        )
        identity = canonical_digest(
            {
                "input_digest": str(input_digest),
                "calibration_digest": str(frequency_center.digest),
                "algorithm_digest": str(algorithm_ref.digest),
                "config_digest": str(config_ref.digest),
                "windows": tuple(item.input_digest for item in windows),
                "evidence": windows,
            }
        ).value
        return StarlinkSymbolwiseReplayBundleV0_1(
            SchemaRef(StarlinkSymbolwiseReplayBundleV0_1.SCHEMA_ID, V0_1),
            f"slsymreplay_{identity[:32]}",
            recording_id,
            recording_identity_digest,
            segment_id,
            receiver_chain_id,
            edge,
            sample_rate_hz,
            segment_sample_count,
            frequency_center,
            algorithm_ref,
            config_ref,
            codebook_digest,
            window_samples,
            cadence_samples,
            tuple(windows),
            plan.analyzed_union_sample_count,
            plan.coverage_fraction,
            plan.timing_search_cell_count,
            plan.refinement_search_cell_count,
            plan.estimated_maximum_working_bytes,
            provenance,
            True,
            (
                "legacy-parity-evidence-not-runtime-dependency",
                "finite-pattern-controls-not-empirical-null",
                "whole-search-calibration-required",
                "known-pilot-not-user-payload",
                "conditioned-roll17-is-not-pattern-symmetric-null",
                "receiver-center-is-explicit-calibration-input",
            ),
        )

    def resource_plan(
        self,
        *,
        sample_rate_hz: float,
        segment_sample_count: int,
        frequency_center: StarlinkReceiverFrequencyCenterV0_1,
    ) -> StarlinkSymbolwiseReplayResourcePlanV0_1:
        """Return and enforce exact replay cost before reading recording bytes."""

        if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be finite and positive")
        if (
            isinstance(segment_sample_count, bool)
            or not isinstance(segment_sample_count, int)
            or segment_sample_count <= 0
        ):
            raise ValueError("segment_sample_count must be a positive integer")
        if not isinstance(frequency_center, StarlinkReceiverFrequencyCenterV0_1):
            raise TypeError("frequency_center must be an immutable v0.1 input")
        window_samples = round(WINDOW_DURATION_S * sample_rate_hz)
        cadence_samples = round(CADENCE_S * sample_rate_hz)
        if window_samples <= 0 or cadence_samples < window_samples:
            raise ValueError("sample rate cannot represent the fixed replay cadence")
        if window_samples > self._config.maximum_window_samples:
            raise ValueError("symbolwise window exceeds maximum_window_samples")
        starts = tuple(
            range(0, segment_sample_count - window_samples + 1, cadence_samples)
        )
        if not starts or len(starts) > self._config.maximum_windows:
            raise ValueError("symbolwise replay window count is empty or unbounded")
        pattern_count = self._config.surrogate_count + 1
        epoch_count = round(sample_rate_hz / FRAME_RATE_HZ)
        timing_cells_per_pattern = epoch_count * len(
            _timing_cfo_grid(frequency_center.center_cfo_hz)
        )
        refinement_cells_per_pattern = _refinement_cell_count(
            frequency_center.center_cfo_hz
        )
        total_timing_cells = timing_cells_per_pattern * pattern_count * len(starts)
        total_refinement_cells = (
            refinement_cells_per_pattern * pattern_count * len(starts)
        )
        if total_timing_cells > self._config.maximum_timing_search_cells:
            raise ValueError("symbolwise replay exceeds timing-search resource ceiling")
        if total_refinement_cells > self._config.maximum_refinement_search_cells:
            raise ValueError("symbolwise replay exceeds refinement resource ceiling")
        estimated_working_bytes = window_samples * (
            np.dtype(np.complex64).itemsize * 8
            + np.dtype(np.complex128).itemsize * 2
            + np.dtype(np.float64).itemsize * 20
        )
        if estimated_working_bytes > self._config.maximum_working_bytes:
            raise ValueError("symbolwise replay exceeds maximum_working_bytes")
        union = len(starts) * window_samples
        return StarlinkSymbolwiseReplayResourcePlanV0_1(
            window_samples,
            cadence_samples,
            starts,
            union,
            union / segment_sample_count,
            pattern_count,
            total_timing_cells,
            total_refinement_cells,
            estimated_working_bytes,
        )

    def _patterns(
        self, sample_rate_hz: float, edge: StarlinkEdge
    ) -> tuple[StarlinkPatternTemplateV0_1, ...]:
        return (
            qin_exact_search_pattern_v0_1(sample_rate_hz, edge),
            *precommitted_surrogate_codebook_v0_1(
                sample_rate_hz,
                edge,
                count=self._config.surrogate_count,
            ),
        )

    def _selection_controls(
        self,
        patterns: tuple[StarlinkPatternTemplateV0_1, ...],
        sample_rate_hz: float,
        edge: StarlinkEdge,
    ) -> tuple[ArtifactRef, ...]:
        qin = qin_edge_pilot_template_pair_v0_1(sample_rate_hz, edge)
        controls = [qin.conditioned_control_ref]
        for pattern in patterns[1:]:
            index = pattern.identity.codebook_index
            assert index is not None
            samples = _surrogate_roll_control(sample_rate_hz, edge, index)
            digest = template_samples_digest(samples)
            controls.append(
                ArtifactRef(
                    f"surrogate-{index:02d}-roll17-{edge.value}-{digest.value[:16]}",
                    digest,
                    SchemaRef("org.leo-flow.starlink-edge-pilot-template", V0_1),
                )
            )
        return tuple(controls)


def starlink_symbolwise_replay_algorithm_ref_v0_1() -> ArtifactRef:
    return ArtifactRef(
        "starlink-pilot-symbolwise-legacy-parity-replay-v0.1",
        canonical_digest(
            {
                "algorithm_id": ALGORITHM_ID,
                "algorithm_version": ALGORITHM_VERSION,
                "historical_oracle_commit": LEGACY_ORACLE_COMMIT,
                "window_duration_s": WINDOW_DURATION_S,
                "cadence_s": CADENCE_S,
                "timing": (
                    "24-symbol-noncoherent-folded-power-all-frame-epochs",
                    "receiver-center-plus-minus-320khz-nine-cell-grid",
                    "four-epochs-separated-by-20-samples",
                ),
                "symbolwise": (
                    "candidate-cfo-plus-minus-100khz-clipped-center-plus-minus-350khz",
                    "mean-normalized-per-symbol-power",
                    "phase-slope-cfo-refinement",
                ),
                "conditioned": "plus-minus-2khz-at-100hz-full-frame-normalized-magnitude",
                "legacy_control": "roll17-conditioned-at-pattern-winner",
                "pattern_controls": "complete-independent-search-per-precommitted-pattern",
                "decision": "none-without-whole-search-calibration",
            }
        ),
        SchemaRef("org.leo-flow.recording-algorithm", V0_1),
    )


def starlink_symbolwise_replay_config_ref_v0_1(
    config: StarlinkSymbolwiseReplayConfigV0_1,
) -> ArtifactRef:
    return ArtifactRef(
        "starlink-symbolwise-replay-config-v0.1",
        canonical_digest(
            {
                "config": config,
                "window_duration_s": WINDOW_DURATION_S,
                "cadence_s": CADENCE_S,
                "timing_search_span_hz": TIMING_SEARCH_SPAN_HZ,
                "timing_search_step_limit_hz": TIMING_SEARCH_STEP_LIMIT_HZ,
                "symbolwise_cfo_limit_hz": SYMBOLWISE_CFO_LIMIT_HZ,
                "symbolwise_cfo_radius_hz": SYMBOLWISE_CFO_RADIUS_HZ,
                "conditioned_cfo_radius_hz": CONDITIONED_CFO_RADIUS_HZ,
                "conditioned_cfo_step_hz": CONDITIONED_CFO_STEP_HZ,
                "anchor_symbol_count": ANCHOR_SYMBOL_COUNT,
                "retained_candidate_count": RETAINED_CANDIDATE_COUNT,
                "candidate_separation_samples": CANDIDATE_SEPARATION_SAMPLES,
                "control_symbol_roll": CONTROL_SYMBOL_ROLL,
            }
        ),
        SchemaRef(CONFIG_SCHEMA_ID, V0_1),
    )


def _analyze_pattern(
    values: np.ndarray,
    sample_rate_hz: float,
    frequency_center_hz: float,
    pattern: StarlinkPatternTemplateV0_1,
    selection_control_ref: ArtifactRef,
) -> StarlinkSymbolwisePatternEvidenceV0_1:
    target = np.asarray(pattern.samples, np.complex64)
    if pattern.identity.role is StarlinkSearchPatternRole.QIN_EXACT:
        pair = qin_edge_pilot_template_pair_v0_1(sample_rate_hz, pattern.identity.edge)
        control = np.asarray(pair.conditioned_control_samples, np.complex64)
    else:
        index = pattern.identity.codebook_index
        assert index is not None
        control = np.asarray(
            _surrogate_roll_control(sample_rate_hz, pattern.identity.edge, index),
            np.complex64,
        )
    timing = _acquire_pattern_epoch(
        values,
        sample_rate_hz,
        target,
        frequency_center_hz,
    )
    candidates: list[
        tuple[
            float,
            float,
            float,
            int,
            _TimingCandidate,
            _SymbolwiseTrack,
            _ConditionedScore,
            _ConditionedScore,
        ]
    ] = []
    for rank, candidate in enumerate(timing):
        coarse_offsets = _symbolwise_cfo_grid(
            candidate.frequency_offset_hz,
            frequency_center_hz,
        )
        track = _track_pattern(
            values,
            sample_rate_hz,
            candidate.epoch_sample,
            target,
            control,
            coarse_offsets,
        )
        offsets = np.arange(
            track.refined_cfo_hz - CONDITIONED_CFO_RADIUS_HZ,
            track.refined_cfo_hz + CONDITIONED_CFO_RADIUS_HZ + 0.1,
            CONDITIONED_CFO_STEP_HZ,
        )
        conditioned = _conditioned_frequency_search(
            values,
            sample_rate_hz,
            candidate.epoch_sample,
            offsets,
            target,
        )
        conditioned_control = _conditioned_score(
            values,
            sample_rate_hz,
            candidate.epoch_sample,
            conditioned.frequency_offset_hz,
            control,
        )
        conditioned_margin = conditioned.score - conditioned_control.score
        symbolwise_margin = track.target_score - track.control_score
        selection_score = max(conditioned_margin, 0.0) * max(symbolwise_margin, 0.0)
        candidates.append(
            (
                selection_score,
                conditioned_margin,
                symbolwise_margin,
                rank,
                candidate,
                track,
                conditioned,
                conditioned_control,
            )
        )
    selected = max(candidates, key=lambda item: (item[0], item[1], item[2]))
    (
        selection_score,
        conditioned_margin,
        symbolwise_margin,
        rank,
        candidate,
        track,
        conditioned,
        conditioned_control,
    ) = selected
    return StarlinkSymbolwisePatternEvidenceV0_1(
        pattern.identity,
        selection_control_ref,
        round(sample_rate_hz / FRAME_RATE_HZ)
        * len(_timing_cfo_grid(frequency_center_hz)),
        _refinement_cell_count(frequency_center_hz),
        len(timing),
        rank,
        candidate.epoch_sample,
        candidate.frequency_offset_hz,
        candidate.folded_score,
        candidate.folded_median,
        candidate.peak_to_median,
        candidate.symbol_frame_support,
        track.coarse_cfo_hz,
        track.residual_cfo_hz,
        conditioned.frequency_offset_hz,
        track.target_score,
        track.control_score,
        symbolwise_margin,
        track.coherence,
        track.control_coherence,
        conditioned.score,
        conditioned_control.score,
        conditioned_margin,
        conditioned.maximum_score,
        conditioned_control.maximum_score,
        conditioned.frame_support,
        track.symbol_match_count,
        selection_score,
    )


def _acquire_pattern_epoch(
    values: np.ndarray,
    sample_rate_hz: float,
    template: np.ndarray,
    frequency_center_hz: float,
) -> tuple[_TimingCandidate, ...]:
    offsets = np.asarray(_timing_cfo_grid(frequency_center_hz), dtype=float)
    period = sample_rate_hz / FRAME_RATE_HZ
    epoch_count = round(period)
    if values.size < round(4 * period):
        raise ValueError("symbolwise replay window requires at least four frames")
    symbol_period = sample_rate_hz * OFDM_SYMBOL_DURATION_S
    anchors = np.unique(np.rint(np.linspace(2, 301, ANCHOR_SYMBOL_COUNT)).astype(int))
    epochs = np.arange(epoch_count)[:, None]
    maximum_frames = int(np.ceil(values.size / period)) + 1
    indexes = np.arange(values.size, dtype=float)
    bank = np.zeros((offsets.size, epoch_count), dtype=float)
    supports = np.zeros((offsets.size, epoch_count), dtype=int)
    for offset_index, offset in enumerate(offsets):
        corrected = values * np.exp(-2j * np.pi * offset * indexes / sample_rate_hz)
        aggregate = np.zeros(epoch_count, dtype=float)
        support = np.zeros(epoch_count, dtype=int)
        for symbol in anchors:
            local_start = round(symbol * symbol_period)
            local_stop = round((symbol + 1) * symbol_period)
            local = template[local_start:local_stop]
            if local.size < 2:
                continue
            correlation = np.correlate(corrected, local, mode="valid")
            energy = np.convolve(
                np.abs(corrected) ** 2, np.ones(local.size), mode="valid"
            )
            denominator = energy * float(np.vdot(local, local).real)
            score = np.zeros(correlation.size, dtype=float)
            usable = denominator > max(float(np.max(denominator)), 0.0) * 1e-12
            np.divide(np.abs(correlation) ** 2, denominator, out=score, where=usable)
            starts = local_start + np.rint(np.arange(maximum_frames) * period).astype(
                int
            )
            sample_indexes = epochs + starts[None, :]
            valid = sample_indexes < score.size
            safe = np.minimum(sample_indexes, score.size - 1)
            aggregate += np.sum(score[safe] * valid, axis=1)
            support += np.sum(valid, axis=1)
        bank[offset_index] = aggregate / np.maximum(support, 1)
        supports[offset_index] = support
    selected_offsets = np.argmax(bank, axis=0)
    folded = bank[selected_offsets, np.arange(epoch_count)]
    median = float(np.median(folded))
    candidates: list[_TimingCandidate] = []
    for epoch_value in np.argsort(folded)[::-1]:
        epoch = int(epoch_value)
        if any(
            min(
                abs(epoch - item.epoch_sample),
                epoch_count - abs(epoch - item.epoch_sample),
            )
            < CANDIDATE_SEPARATION_SAMPLES
            for item in candidates
        ):
            continue
        offset_index = int(selected_offsets[epoch])
        candidates.append(
            _TimingCandidate(
                epoch,
                float(offsets[offset_index]),
                float(folded[epoch]),
                median,
                float(folded[epoch] / max(median, 1e-20)),
                int(supports[offset_index, epoch]),
            )
        )
        if len(candidates) >= RETAINED_CANDIDATE_COUNT:
            break
    if not candidates:
        raise ValueError("symbolwise timing search produced no candidate")
    return tuple(candidates)


def _track_pattern(
    values: np.ndarray,
    sample_rate_hz: float,
    epoch_sample: int,
    target: np.ndarray,
    control: np.ndarray,
    coarse_frequency_offsets_hz: tuple[float, ...],
) -> _SymbolwiseTrack:
    candidates = []
    for offset in coarse_frequency_offsets_hz:
        correlations, powers, times = _symbol_correlations(
            values,
            sample_rate_hz,
            epoch_sample,
            offset,
            target,
        )
        candidates.append(
            (
                float(np.mean(powers)) if powers.size else 0.0,
                offset,
                correlations,
                times,
            )
        )
    _, coarse_offset, correlations, times = max(candidates, key=lambda item: item[0])
    usable = (
        np.abs(correlations) > np.median(np.abs(correlations)) * 0.25
        if correlations.size
        else np.zeros(0, dtype=bool)
    )
    if np.count_nonzero(usable) >= 3:
        phase = np.unwrap(np.angle(correlations[usable]))
        residual_hz = float(np.polyfit(times[usable], phase, 1)[0] / (2 * np.pi))
        unique = sorted(set(coarse_frequency_offsets_hz))
        coarse_step = min(np.diff(unique)) if len(unique) > 1 else math.inf
        residual_hz = float(np.clip(residual_hz, -coarse_step / 2, coarse_step / 2))
    else:
        residual_hz = 0.0
    refined = coarse_offset + residual_hz
    target_correlations, target_powers, _ = _symbol_correlations(
        values, sample_rate_hz, epoch_sample, refined, target
    )
    control_correlations, control_powers, _ = _symbol_correlations(
        values, sample_rate_hz, epoch_sample, refined, control
    )
    target_score = float(np.mean(target_powers)) if target_powers.size else 0.0
    control_score = float(np.mean(control_powers)) if control_powers.size else 0.0
    coherence = (
        float(
            abs(np.sum(target_correlations))
            / max(np.sum(np.abs(target_correlations)), 1e-20)
        )
        if target_correlations.size
        else 0.0
    )
    control_coherence = (
        float(
            abs(np.sum(control_correlations))
            / max(np.sum(np.abs(control_correlations)), 1e-20)
        )
        if control_correlations.size
        else 0.0
    )
    return _SymbolwiseTrack(
        coarse_offset,
        residual_hz,
        refined,
        target_score,
        control_score,
        coherence,
        control_coherence,
        len(target_powers),
    )


def _symbol_correlations(
    values: np.ndarray,
    sample_rate_hz: float,
    epoch_sample: int,
    frequency_offset_hz: float,
    template: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    period = sample_rate_hz / FRAME_RATE_HZ
    symbol_period = sample_rate_hz * OFDM_SYMBOL_DURATION_S
    starts: list[int] = []
    local_starts: list[int] = []
    counts: list[int] = []
    times: list[float] = []
    frame = 0
    while epoch_sample + round(frame * period) < values.size:
        frame_start = epoch_sample + round(frame * period)
        for symbol in range(2, 302):
            start = frame_start + round(symbol * symbol_period)
            stop = frame_start + round((symbol + 1) * symbol_period)
            local_start = round(symbol * symbol_period)
            local_stop = round((symbol + 1) * symbol_period)
            count = min(stop - start, local_stop - local_start)
            if start < 0 or start + count > values.size or count < 2:
                continue
            starts.append(start)
            local_starts.append(local_start)
            counts.append(count)
            times.append((start + (count - 1) / 2) / sample_rate_hz)
        frame += 1
    if not starts:
        return (
            np.empty(0, np.complex64),
            np.empty(0, float),
            np.empty(0, float),
        )
    width = max(counts)
    columns = np.arange(width)[None, :]
    sample_indexes = np.asarray(starts)[:, None] + columns
    local_indexes = np.asarray(local_starts)[:, None] + columns
    mask = columns < np.asarray(counts)[:, None]
    local = template[np.minimum(local_indexes, template.size - 1)] * mask
    selected = values[np.minimum(sample_indexes, values.size - 1)] * mask
    corrected = selected * np.exp(
        -2j * np.pi * frequency_offset_hz * sample_indexes / sample_rate_hz
    )
    correlations = np.sum(np.conj(local) * corrected, axis=1)
    denominator = np.sum(np.abs(local) ** 2, axis=1) * np.sum(
        np.abs(corrected) ** 2, axis=1
    )
    powers = np.abs(correlations) ** 2 / np.maximum(denominator, 1e-20)
    return (
        np.asarray(correlations, np.complex64),
        np.asarray(powers, float),
        np.asarray(times, float),
    )


def _conditioned_frequency_search(
    values: np.ndarray,
    sample_rate_hz: float,
    epoch_sample: int,
    frequency_offsets_hz: np.ndarray,
    template: np.ndarray,
) -> _ConditionedScore:
    reports = tuple(
        _conditioned_score(
            values,
            sample_rate_hz,
            epoch_sample,
            float(offset),
            template,
        )
        for offset in frequency_offsets_hz
    )
    return max(reports, key=lambda item: item.score)


def _conditioned_score(
    values: np.ndarray,
    sample_rate_hz: float,
    epoch_sample: int,
    frequency_offset_hz: float,
    template: np.ndarray,
) -> _ConditionedScore:
    template_energy = float(np.vdot(template, template).real)
    period = sample_rate_hz / FRAME_RATE_HZ
    scores = []
    frame = 0
    while True:
        start = epoch_sample + round(frame * period)
        if start + template.size > values.size:
            break
        indexes = np.arange(start, start + template.size)
        corrected = values[start : start + template.size] * np.exp(
            -2j * np.pi * frequency_offset_hz * indexes / sample_rate_hz
        )
        denominator = template_energy * float(np.vdot(corrected, corrected).real)
        scores.append(
            0.0
            if denominator <= 0
            else float(abs(np.vdot(template, corrected)) / math.sqrt(denominator))
        )
        frame += 1
    return _ConditionedScore(
        float(np.mean(scores)) if scores else 0.0,
        float(max(scores)) if scores else 0.0,
        len(scores),
        frequency_offset_hz,
    )


def _timing_cfo_grid(frequency_center_hz: float) -> tuple[float, ...]:
    count = math.ceil(TIMING_SEARCH_SPAN_HZ / TIMING_SEARCH_STEP_LIMIT_HZ)
    return tuple(
        float(value + frequency_center_hz)
        for value in np.linspace(
            -TIMING_SEARCH_SPAN_HZ,
            TIMING_SEARCH_SPAN_HZ,
            2 * count + 1,
        )
    )


def _symbolwise_cfo_grid(
    initial_cfo_hz: float,
    frequency_center_hz: float,
) -> tuple[float, ...]:
    return tuple(
        sorted(
            {
                float(
                    np.clip(
                        initial_cfo_hz + delta,
                        frequency_center_hz - SYMBOLWISE_CFO_LIMIT_HZ,
                        frequency_center_hz + SYMBOLWISE_CFO_LIMIT_HZ,
                    )
                )
                for delta in (
                    -SYMBOLWISE_CFO_RADIUS_HZ,
                    0.0,
                    SYMBOLWISE_CFO_RADIUS_HZ,
                )
            }
        )
    )


def _refinement_cell_count(frequency_center_hz: float) -> int:
    return sum(
        len(_symbolwise_cfo_grid(value, frequency_center_hz))
        + 2
        + round(2 * CONDITIONED_CFO_RADIUS_HZ / CONDITIONED_CFO_STEP_HZ)
        + 1
        + 1
        for value in _timing_cfo_grid(frequency_center_hz)[:RETAINED_CANDIDATE_COUNT]
    )


@lru_cache(maxsize=32)
def _surrogate_roll_control(
    sample_rate_hz: float,
    edge: StarlinkEdge,
    codebook_index: int,
) -> tuple[complex, ...]:
    states = np.asarray(precommitted_surrogate_states_v0_1(codebook_index), dtype=int)
    rolled = np.roll(states, CONTROL_SYMBOL_ROLL, axis=0)
    samples = _synthesize_edge_frame(
        sample_rate_hz,
        edge,
        tuple(tuple(int(value) for value in row) for row in rolled),
    )
    reference = qin_edge_pilot_template_pair_v0_1(sample_rate_hz, edge).exact_samples
    source_energy = math.fsum(abs(value) ** 2 for value in samples)
    reference_energy = math.fsum(abs(value) ** 2 for value in reference)
    scale = math.sqrt(reference_energy / source_energy)
    return tuple(_complex64(value * scale) for value in samples)


def _synthesize_edge_frame(
    sample_rate_hz: float,
    edge: StarlinkEdge,
    states: tuple[tuple[int, ...], ...],
) -> tuple[complex, ...]:
    indices = qin_edge_pilot_indices_v1(edge)
    tuning_offset_hz = math.fsum(_subcarrier_offset_hz(index) for index in indices) / 8
    symbols = tuple(
        tuple(_complex64(cmath.exp(0.5j * math.pi * (state + 0.5))) for state in row)
        for row in states
    )
    output = []
    for sample_index in range(round(sample_rate_hz / FRAME_RATE_HZ)):
        time_s = sample_index / sample_rate_hz
        symbol_index = math.floor(time_s / OFDM_SYMBOL_DURATION_S)
        if symbol_index < 2 or symbol_index > 301:
            output.append(0j)
            continue
        local_time_s = time_s - symbol_index * OFDM_SYMBOL_DURATION_S
        value = 0j
        for column, subcarrier in enumerate(indices):
            frequency_hz = _subcarrier_offset_hz(subcarrier) - tuning_offset_hz
            value += symbols[symbol_index - 2][column] * cmath.exp(
                2j * math.pi * frequency_hz * (local_time_s - CYCLIC_PREFIX_DURATION_S)
            )
        output.append(_complex64(value / math.sqrt(8)))
    return tuple(output)


def _subcarrier_offset_hz(index: int) -> float:
    signed = index if index < 512 else index - 1024
    return signed * SUBCARRIER_SPACING_HZ


def _complex64(value: complex) -> complex:
    real = 0.0 if abs(value.real) < 1e-10 else value.real
    imag = 0.0 if abs(value.imag) < 1e-10 else value.imag
    return complex(
        struct.unpack("!f", struct.pack("!f", real))[0],
        struct.unpack("!f", struct.pack("!f", imag))[0],
    )


def _window_digest(values: np.ndarray) -> Digest:
    canonical = np.asarray(values, dtype="<c8")
    return Digest.sha256(canonical.tobytes(order="C"))
