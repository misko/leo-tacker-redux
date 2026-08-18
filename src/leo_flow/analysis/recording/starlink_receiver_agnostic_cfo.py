"""Bounded receiver-agnostic, pattern-symmetric residual-CFO search v0.6."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from typing import Protocol, cast

import numpy as np

from leo_flow.analysis.qam_goodness import qam_goodness_v0_2
from leo_flow.contracts.core import ArtifactRef, Provenance, SchemaRef, canonical_digest
from leo_flow.contracts.starlink_adaptive_calibration import AdaptivePatternRole
from leo_flow.contracts.starlink_receiver_agnostic_cfo import (
    MAXIMUM_CFO_PATTERNS,
    V0_6,
    ReceiverAgnosticCfoCellStage,
    ReceiverAgnosticCfoCellV0_6,
    ReceiverAgnosticCfoPatternV0_6,
    ReceiverAgnosticCfoQamWindowBundleV0_6,
    ReceiverAgnosticCfoSearchPlanV0_6,
    ReceiverAgnosticCfoSearchReceiptV0_6,
    ReceiverAgnosticCfoWindowV0_6,
    ReceiverAgnosticCfoWinnerV0_6,
    ReceiverAgnosticPatternQamEvidenceV0_6,
)

from .api import AnalysisExecutionContext
from .starlink import FRAME_RATE_HZ
from .starlink_acquisition import (
    DEFAULT_ACQUIRE_SYMBOLS,
    normalized_frame_score_v0_3,
)
from .starlink_pattern_symmetric_qam import known_pattern_qam_quality_v0_5
from .starlink_surrogate_null import (
    StarlinkPatternTemplateV0_1,
    conditioned_pattern_control_v0_1,
    precommitted_surrogate_codebook_v0_1,
    precommitted_surrogate_states_v0_1,
    qin_exact_search_pattern_v0_1,
)
from .starlink_templates import qin_edge_pilot_states_v1

RAW_IQ_SCORER_MAXIMUM_WINDOW_SAMPLES = 50_000
RAW_IQ_SCORER_MAXIMUM_WORKING_BYTES = 64 * 1024 * 1024
RAW_IQ_QAM_MAXIMUM_PATTERNS = 9


class ReceiverAgnosticCfoScorerV0_6(Protocol):
    def score(
        self,
        pattern: ReceiverAgnosticCfoPatternV0_6,
        epoch_sample: int,
        cfo_hz: float,
    ) -> float: ...

class ReceiverAgnosticCfoRawIqScorerV0_6:
    """Narrow raw-IQ adapter for the immutable v0.3 normalized-frame score."""

    def __init__(
        self,
        samples: Sequence[complex],
        sample_rate_hz: float,
        patterns: tuple[StarlinkPatternTemplateV0_1, ...],
        *,
        maximum_window_samples: int = RAW_IQ_SCORER_MAXIMUM_WINDOW_SAMPLES,
        maximum_working_bytes: int = RAW_IQ_SCORER_MAXIMUM_WORKING_BYTES,
    ) -> None:
        values = np.asarray(samples, dtype=np.complex128)
        if (
            values.ndim != 1
            or values.size <= 0
            or values.size > maximum_window_samples
            or not np.all(np.isfinite(values))
            or not math.isfinite(sample_rate_hz)
            or sample_rate_hz <= 0
        ):
            raise ValueError("raw-IQ CFO scorer input is invalid or exceeds its bound")
        if (
            isinstance(maximum_working_bytes, bool)
            or not isinstance(maximum_working_bytes, int)
            or maximum_working_bytes <= 0
            or values.nbytes * 4 > maximum_working_bytes
        ):
            raise ValueError("raw-IQ CFO scorer exceeds its working-byte bound")
        by_digest = {item.identity.template_ref.digest: item for item in patterns}
        if not patterns or len(by_digest) != len(patterns):
            raise ValueError("raw-IQ CFO scorer patterns are empty or duplicated")
        self._values = values
        self._sample_rate_hz = float(sample_rate_hz)
        self._patterns = by_digest

    def score(
        self,
        pattern: ReceiverAgnosticCfoPatternV0_6,
        epoch_sample: int,
        cfo_hz: float,
    ) -> float:
        bound = self._bound_pattern(pattern)
        score, _ = normalized_frame_score_v0_3(
            self._values,
            np.asarray(bound.samples, dtype=np.complex128),
            self._sample_rate_hz,
            epoch_sample,
            cfo_hz,
            DEFAULT_ACQUIRE_SYMBOLS,
        )
        return score

    def _bound_pattern(
        self, pattern: ReceiverAgnosticCfoPatternV0_6
    ) -> StarlinkPatternTemplateV0_1:
        bound = self._patterns.get(pattern.template_digest)
        if bound is None:
            raise ValueError("CFO scorer received an undeclared pattern")
        expected_role = (
            "qin-exact" if pattern.pattern_index == 0 else "precommitted-surrogate"
        )
        if bound.identity.role.value != expected_role:
            raise ValueError("CFO scorer pattern role differs from its template")
        return bound


class ReceiverAgnosticCfoQamAnalyzerV0_6:
    """Run symmetric raw-IQ CFO search and v0.5 QAM on one exact window."""

    def __init__(
        self,
        plan: ReceiverAgnosticCfoSearchPlanV0_6,
        *,
        maximum_window_samples: int = RAW_IQ_SCORER_MAXIMUM_WINDOW_SAMPLES,
        maximum_working_bytes: int = RAW_IQ_SCORER_MAXIMUM_WORKING_BYTES,
        maximum_patterns: int = RAW_IQ_QAM_MAXIMUM_PATTERNS,
    ) -> None:
        if (
            isinstance(maximum_window_samples, bool)
            or not isinstance(maximum_window_samples, int)
            or maximum_window_samples <= 0
            or isinstance(maximum_patterns, bool)
            or not isinstance(maximum_patterns, int)
            or not 1 <= maximum_patterns <= RAW_IQ_QAM_MAXIMUM_PATTERNS
        ):
            raise ValueError("raw-IQ CFO/QAM analyzer resource policy is invalid")
        self._plan = plan
        self._maximum_window_samples = maximum_window_samples
        self._maximum_working_bytes = maximum_working_bytes
        self._maximum_patterns = maximum_patterns

    def analyze(
        self,
        samples: Sequence[complex],
        window: ReceiverAgnosticCfoWindowV0_6,
        *,
        pattern_count: int,
        execution: AnalysisExecutionContext,
    ) -> ReceiverAgnosticCfoQamWindowBundleV0_6:
        if (
            isinstance(pattern_count, bool)
            or not isinstance(pattern_count, int)
            or not 1 <= pattern_count <= self._maximum_patterns
        ):
            raise ValueError("raw-IQ CFO/QAM pattern count exceeds its bound")
        values = np.asarray(samples, dtype=np.complex128)
        if values.ndim != 1 or values.size != window.sample_count:
            raise ValueError("raw-IQ samples differ from the declared exact window")
        if values.size > self._maximum_window_samples:
            raise ValueError("raw-IQ window exceeds its sample bound")
        patterns = (
            qin_exact_search_pattern_v0_1(window.sample_rate_hz, window.edge),
            *precommitted_surrogate_codebook_v0_1(
                window.sample_rate_hz, window.edge, count=pattern_count - 1
            ),
        )
        declared = tuple(
            ReceiverAgnosticCfoPatternV0_6(
                index,
                AdaptivePatternRole.QIN
                if index == 0
                else AdaptivePatternRole.SURROGATE,
                pattern.identity.template_ref.digest,
            )
            for index, pattern in enumerate(patterns)
        )
        scorer = ReceiverAgnosticCfoRawIqScorerV0_6(
            cast(Sequence[complex], values),
            window.sample_rate_hz,
            patterns,
            maximum_window_samples=self._maximum_window_samples,
            maximum_working_bytes=self._maximum_working_bytes,
        )
        epoch_modulus = round(window.sample_rate_hz / FRAME_RATE_HZ)
        receipt = receiver_agnostic_cfo_search_v0_6(
            self._plan, epoch_modulus, declared, scorer
        )
        qam = []
        for pattern, declared_pattern, winner in zip(
            patterns, declared, receipt.winners, strict=True
        ):
            states = (
                qin_edge_pilot_states_v1(window.edge)
                if declared_pattern.pattern_index == 0
                else precommitted_surrogate_states_v0_1(
                    declared_pattern.pattern_index - 1
                )
            )
            accuracy, evm, support = known_pattern_qam_quality_v0_5(
                cast(Sequence[complex], values),
                window.sample_rate_hz,
                window.edge,
                states,
                winner.epoch_sample,
                winner.cfo_hz,
            )
            control = conditioned_pattern_control_v0_1(pattern)
            qam.append(
                ReceiverAgnosticPatternQamEvidenceV0_6(
                    declared_pattern.pattern_index,
                    declared_pattern.role,
                    pattern.identity.template_ref,
                    control.template_ref,
                    winner,
                    support,
                    accuracy,
                    evm,
                    qam_goodness_v0_2(accuracy, evm),
                )
            )
        search_algorithm_ref = receiver_agnostic_cfo_search_algorithm_ref_v0_6()
        scorer_algorithm_ref = receiver_agnostic_cfo_raw_iq_scorer_ref_v0_6()
        qam_algorithm_ref = receiver_agnostic_known_pattern_qam_ref_v0_6()
        config_ref = receiver_agnostic_cfo_config_ref_v0_6(self._plan)
        dependencies = (
            search_algorithm_ref.digest,
            scorer_algorithm_ref.digest,
            qam_algorithm_ref.digest,
            config_ref.digest,
            *(item.template_ref.digest for item in qam),
            *(item.control_template_ref.digest for item in qam),
        )
        provenance = Provenance(
            execution.producer_name,
            execution.producer_version,
            execution.git_commit,
            execution.environment_digest,
            config_ref.digest,
            (
                window.recording_identity_digest,
                window.source_recording_ref.digest,
                window.source_window_ref.digest,
            ),
            dependencies,
            execution.started_utc_ns,
            execution.completed_utc_ns,
            execution.host_class,
        )
        identity = canonical_digest(
            {"window": window.digest, "search": receipt.digest, "qam": qam}
        ).value
        return ReceiverAgnosticCfoQamWindowBundleV0_6(
            SchemaRef(ReceiverAgnosticCfoQamWindowBundleV0_6.SCHEMA_ID, V0_6),
            f"slcfoqam6_{identity[:32]}",
            window,
            search_algorithm_ref,
            scorer_algorithm_ref,
            qam_algorithm_ref,
            config_ref,
            receipt,
            tuple(qam),
            provenance,
            True,
            None,
            (
                "candidate-evidence-not-calibrated-detection",
                "identical-raw-iq-window-for-every-pattern",
                "known-pattern-qam-at-independent-pattern-winner",
                "no-lnb-label-center-or-receiver-correction",
                "retro-and-j1-are-conditioned-numerical-canaries-only",
            ),
        )


def receiver_agnostic_cfo_search_algorithm_ref_v0_6() -> ArtifactRef:
    return ArtifactRef(
        "receiver-agnostic-pattern-symmetric-cfo-search-v0.6",
        canonical_digest(
            {
                "search": "endpoint-coarse-equal-basin-union-local",
                "pattern_policy": "every-pattern-on-every-unique-cell",
                "receiver_adjustment": "none",
            }
        ),
        SchemaRef("org.leo-flow.recording-algorithm", V0_6),
    )


def receiver_agnostic_cfo_raw_iq_scorer_ref_v0_6() -> ArtifactRef:
    return ArtifactRef(
        "starlink-v0.3-normalized-frame-raw-iq-scorer-v0.6",
        canonical_digest(
            {
                "statistic": "starlink-acquisition-v0.3-normalized-frame-score",
                "symbols": DEFAULT_ACQUIRE_SYMBOLS,
                "inter_frame_combination": "noncoherent-mean",
            }
        ),
        SchemaRef("org.leo-flow.recording-algorithm", V0_6),
    )


def receiver_agnostic_known_pattern_qam_ref_v0_6() -> ArtifactRef:
    return ArtifactRef(
        "starlink-known-pattern-qam-v0.5-at-v0.6-winners",
        canonical_digest(
            {
                "quality": "known_pattern_qam_quality_v0_5",
                "winner_mapping": "independent-per-pattern-v0.6-winner",
                "window_mapping": "identical-exact-raw-iq-window",
            }
        ),
        SchemaRef("org.leo-flow.recording-algorithm", V0_6),
    )


def receiver_agnostic_cfo_config_ref_v0_6(
    plan: ReceiverAgnosticCfoSearchPlanV0_6,
) -> ArtifactRef:
    return ArtifactRef(
        "receiver-agnostic-cfo-search-plan-v0.6",
        plan.digest,
        SchemaRef("org.leo-flow.receiver-agnostic-cfo-search-plan", V0_6),
    )


def receiver_agnostic_cfo_search_v0_6(
    plan: ReceiverAgnosticCfoSearchPlanV0_6,
    epoch_modulus_samples: int,
    patterns: tuple[ReceiverAgnosticCfoPatternV0_6, ...],
    scorer: ReceiverAgnosticCfoScorerV0_6,
) -> ReceiverAgnosticCfoSearchReceiptV0_6:
    """Search one plan without accepting receiver labels or frequency corrections."""
    if epoch_modulus_samples <= 0:
        raise ValueError("epoch modulus must be positive")
    if not patterns or tuple(item.pattern_index for item in patterns) != tuple(
        range(len(patterns))
    ) or len(patterns) > MAXIMUM_CFO_PATTERNS or len(
        {item.template_digest for item in patterns}
    ) != len(patterns):
        raise ValueError("CFO patterns must be canonical")

    coarse_cfo_count = _endpoint_grid_count(
        plan.cfo_min_hz, plan.cfo_max_hz, plan.coarse_cfo_step_hz
    )
    coarse_epoch_count = _integer_endpoint_grid_count(
        0, epoch_modulus_samples - 1, plan.coarse_epoch_stride_samples
    )
    if coarse_cfo_count * coarse_epoch_count > plan.maximum_coarse_cells:
        raise ValueError("declared coarse CFO search exceeds its cell bound")
    coarse_cfos = _endpoint_grid(
        plan.cfo_min_hz, plan.cfo_max_hz, plan.coarse_cfo_step_hz
    )
    coarse_epochs = _integer_endpoint_grid(
        0, epoch_modulus_samples - 1, plan.coarse_epoch_stride_samples
    )
    coarse_coordinates = tuple(
        (epoch, cfo) for cfo in coarse_cfos for epoch in coarse_epochs
    )
    coarse_scores = _evaluate(coarse_coordinates, patterns, scorer)

    centers_by_pattern: list[tuple[tuple[int, float], ...]] = []
    for pattern_index in range(len(patterns)):
        ranked = sorted(
            coarse_coordinates,
            key=lambda coordinate: (
                -coarse_scores[coordinate][pattern_index],
                abs(coordinate[1]),
                coordinate[1],
                coordinate[0],
            ),
        )
        retained: list[tuple[int, float]] = []
        for coordinate in ranked:
            if all(
                _circular_distance(
                    coordinate[0], existing[0], epoch_modulus_samples
                ) >= plan.basin_epoch_separation_samples
                or abs(coordinate[1] - existing[1])
                >= plan.basin_cfo_separation_hz
                for existing in retained
            ):
                retained.append(coordinate)
                if len(retained) == plan.basins_per_pattern:
                    break
        if len(retained) != plan.basins_per_pattern:
            raise ValueError("coarse geometry cannot supply declared basin quota")
        centers_by_pattern.append(tuple(retained))

    local_selectors: dict[tuple[int, float], set[int]] = defaultdict(set)
    for pattern_index, centers in enumerate(centers_by_pattern):
        for center_epoch, center_cfo in centers:
            epochs = tuple(
                sorted(
                    {
                        (center_epoch + delta) % epoch_modulus_samples
                        for delta in range(
                            -plan.local_epoch_radius_samples,
                            plan.local_epoch_radius_samples + 1,
                        )
                    }
                )
            )
            cfos = _endpoint_grid(
                max(plan.cfo_min_hz, center_cfo - plan.local_cfo_radius_hz),
                min(plan.cfo_max_hz, center_cfo + plan.local_cfo_radius_hz),
                plan.local_cfo_step_hz,
            )
            for cfo in cfos:
                for epoch in epochs:
                    local_selectors[(epoch, cfo)].add(pattern_index)
                    if len(local_selectors) > plan.maximum_local_cells:
                        raise ValueError(
                            "declared local CFO search exceeds its cell bound"
                        )
    if len(local_selectors) > plan.maximum_local_cells:
        raise ValueError("declared local CFO search exceeds its cell bound")

    all_coordinates = tuple(
        sorted(
            set(coarse_coordinates) | set(local_selectors),
            key=lambda coordinate: (coordinate[1], coordinate[0]),
        )
    )
    evaluations = len(all_coordinates) * len(patterns)
    if len(all_coordinates) > plan.maximum_unique_cells:
        raise ValueError("declared CFO search exceeds its unique-cell bound")
    if evaluations > plan.maximum_pattern_evaluations:
        raise ValueError("declared CFO search exceeds its pattern-evaluation bound")

    local_only = tuple(
        coordinate for coordinate in all_coordinates if coordinate not in coarse_scores
    )
    scores = dict(coarse_scores)
    scores.update(_evaluate(local_only, patterns, scorer))
    cells = tuple(
        ReceiverAgnosticCfoCellV0_6(
            index,
            ReceiverAgnosticCfoCellStage.COARSE
            if coordinate in coarse_scores
            else ReceiverAgnosticCfoCellStage.LOCAL,
            coordinate[0],
            coordinate[1],
            tuple(sorted(local_selectors.get(coordinate, ()))),
            scores[coordinate],
        )
        for index, coordinate in enumerate(all_coordinates)
    )
    winners = []
    for pattern_index in range(len(patterns)):
        cell = min(
            cells,
            key=lambda item: (
                -item.pattern_scores[pattern_index],
                abs(item.cfo_hz),
                item.cfo_hz,
                item.epoch_sample,
            ),
        )
        winners.append(
            ReceiverAgnosticCfoWinnerV0_6(
                pattern_index,
                cell.cell_index,
                cell.epoch_sample,
                cell.cfo_hz,
                cell.pattern_scores[pattern_index],
            )
        )
    return ReceiverAgnosticCfoSearchReceiptV0_6(
        SchemaRef(ReceiverAgnosticCfoSearchReceiptV0_6.SCHEMA_ID, V0_6),
        plan,
        epoch_modulus_samples,
        patterns,
        cells,
        tuple(winners),
        len(coarse_coordinates),
        len(local_selectors),
        len(cells),
        evaluations,
        evaluations,
        True,
        None,
        (
            "candidate-evidence-not-calibrated-detection",
            "identical-residual-cfo-domain-for-every-radio-rx",
            "look-elsewhere-family-is-exact-pattern-by-unique-cell-product",
            "no-detection-threshold-or-calibration-claim",
            "qin-and-surrogates-search-identical-cell-union",
            "retro-and-j1-are-conditioned-numerical-canaries-only",
        ),
    )


def _evaluate(
    coordinates: tuple[tuple[int, float], ...],
    patterns: tuple[ReceiverAgnosticCfoPatternV0_6, ...],
    scorer: ReceiverAgnosticCfoScorerV0_6,
) -> dict[tuple[int, float], tuple[float, ...]]:
    result = {}
    for coordinate in coordinates:
        row = tuple(
            float(scorer.score(pattern, coordinate[0], coordinate[1]))
            for pattern in patterns
        )
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in row):
            raise ValueError("CFO scorer returned a value outside [0,1]")
        result[coordinate] = row
    return result


def _endpoint_grid(low: float, high: float, step: float) -> tuple[float, ...]:
    count = math.floor((high - low) / step)
    values = [float(low + index * step) for index in range(count + 1)]
    if not math.isclose(values[-1], high, rel_tol=0, abs_tol=1e-9):
        values.append(float(high))
    else:
        values[-1] = float(high)
    return tuple(values)


def _endpoint_grid_count(low: float, high: float, step: float) -> int:
    quotient = (high - low) / step
    if not math.isfinite(quotient):
        raise ValueError("CFO grid geometry is not finite")
    count = math.floor(quotient) + 1
    endpoint = low + (count - 1) * step
    return count if math.isclose(endpoint, high, rel_tol=0, abs_tol=1e-9) else count + 1


def _integer_endpoint_grid(low: int, high: int, step: int) -> tuple[int, ...]:
    values = list(range(low, high + 1, step))
    if values[-1] != high:
        values.append(high)
    return tuple(values)


def _integer_endpoint_grid_count(low: int, high: int, step: int) -> int:
    quotient, remainder = divmod(high - low, step)
    return quotient + 1 + int(remainder != 0)


def _circular_distance(left: int, right: int, modulus: int) -> int:
    direct = abs(left - right)
    return min(direct, modulus - direct)
