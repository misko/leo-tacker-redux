"""Bounded receiver-agnostic, pattern-symmetric residual-CFO search v0.6."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Protocol

from leo_flow.contracts.core import SchemaRef
from leo_flow.contracts.starlink_receiver_agnostic_cfo import (
    MAXIMUM_CFO_PATTERNS,
    V0_6,
    ReceiverAgnosticCfoCellStage,
    ReceiverAgnosticCfoCellV0_6,
    ReceiverAgnosticCfoPatternV0_6,
    ReceiverAgnosticCfoSearchPlanV0_6,
    ReceiverAgnosticCfoSearchReceiptV0_6,
    ReceiverAgnosticCfoWinnerV0_6,
)


class ReceiverAgnosticCfoScorerV0_6(Protocol):
    def score(
        self,
        pattern: ReceiverAgnosticCfoPatternV0_6,
        epoch_sample: int,
        cfo_hz: float,
    ) -> float: ...


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
