"""Deterministic legacy-style, pattern-symmetric time-refinement planning."""

from __future__ import annotations

from collections import defaultdict

from leo_flow.contracts.core import ArtifactRef, SchemaRef
from leo_flow.contracts.starlink_adaptive_refinement import (
    V0_1,
    AdaptiveWindowStage,
    StarlinkAdaptiveBaseWindowV0_1,
    StarlinkAdaptiveExactWindowV0_1,
    StarlinkAdaptivePatternScoreV0_1,
    StarlinkAdaptiveRefinementPlanV0_1,
    StarlinkAdaptiveRefinementSelectionV0_1,
)


def adaptive_base_windows_v0_1(
    segment_sample_count: int,
    plan: StarlinkAdaptiveRefinementPlanV0_1,
    power_seed_intervals: tuple[tuple[int, int, int], ...],
) -> tuple[StarlinkAdaptiveBaseWindowV0_1, ...]:
    """Combine fixed endpoint-preserving sentinels and pattern-blind power seeds."""
    if segment_sample_count < plan.probe_sample_count:
        raise ValueError("adaptive probe exceeds its segment")
    if len(power_seed_intervals) > plan.maximum_power_seeds:
        raise ValueError("adaptive power seeds exceed their declared bound")
    last = segment_sample_count - plan.probe_sample_count
    sentinel_starts = list(range(0, last + 1, plan.sentinel_stride_samples))
    if sentinel_starts[-1] != last:
        sentinel_starts.append(last)
    reasons: dict[int, set[str]] = defaultdict(set)
    ranks: dict[int, set[int]] = defaultdict(set)
    for start in sentinel_starts:
        reasons[start].add("fixed-sentinel")
    seen_ranks: set[int] = set()
    for rank, seed_start, seed_stop in power_seed_intervals:
        if (
            rank < 0
            or rank in seen_ranks
            or seed_start < 0
            or seed_stop <= seed_start
            or seed_stop > segment_sample_count
        ):
            raise ValueError("adaptive power seed is invalid")
        seen_ranks.add(rank)
        center = (seed_start + seed_stop) // 2
        start = min(last, max(0, center - plan.probe_sample_count // 2))
        reasons[start].add("pattern-blind-power-seed")
        ranks[start].add(rank)
    if len(reasons) > plan.maximum_base_windows:
        raise ValueError("adaptive base windows exceed their declared bound")
    return tuple(
        StarlinkAdaptiveBaseWindowV0_1(
            start,
            start + plan.probe_sample_count,
            tuple(sorted(reasons[start])),
            tuple(sorted(ranks[start])),
        )
        for start in sorted(reasons)
    )


def adaptive_refinement_selection_v0_1(
    segment_sample_count: int,
    plan: StarlinkAdaptiveRefinementPlanV0_1,
    base_windows: tuple[StarlinkAdaptiveBaseWindowV0_1, ...],
    pattern_scores: tuple[StarlinkAdaptivePatternScoreV0_1, ...],
) -> StarlinkAdaptiveRefinementSelectionV0_1:
    """Expand the same top-score quota for every pattern, then search its union."""
    base_starts = tuple(item.start_sample for item in base_windows)
    if not base_starts or base_starts != tuple(sorted(set(base_starts))):
        raise ValueError("adaptive base windows are noncanonical")
    grouped: dict[ArtifactRef, dict[int, float]] = defaultdict(dict)
    for item in pattern_scores:
        scores = grouped[item.pattern_ref]
        if item.start_sample in scores:
            raise ValueError("adaptive pattern/base score is duplicated")
        scores[item.start_sample] = item.score
    pattern_refs = tuple(sorted(grouped, key=lambda item: str(item.digest)))
    if not pattern_refs or any(
        tuple(sorted(grouped[item])) != base_starts for item in pattern_refs
    ):
        raise ValueError("every pattern must score every adaptive base window")
    last = segment_sample_count - plan.probe_sample_count
    selected_by_start: dict[int, set[ArtifactRef]] = defaultdict(set)
    for pattern_ref in pattern_refs:
        ranked = sorted(
            grouped[pattern_ref].items(), key=lambda item: (-item[1], item[0])
        )[: plan.candidate_centers_per_pattern]
        for center_start, _ in ranked:
            low = max(0, center_start - plan.local_radius_samples)
            high = min(last, center_start + plan.local_radius_samples)
            starts = list(range(low, high + 1, plan.local_stride_samples))
            if starts[-1] != high:
                starts.append(high)
            for start in starts:
                selected_by_start[start].add(pattern_ref)
    base_by_start = {item.start_sample: item for item in base_windows}
    all_starts = tuple(sorted(set(base_starts) | set(selected_by_start)))
    if len(all_starts) > plan.maximum_exact_windows:
        raise ValueError("adaptive local union exceeds its declared bound")
    exact = []
    for index, start in enumerate(all_starts):
        base = base_by_start.get(start)
        exact.append(
            StarlinkAdaptiveExactWindowV0_1(
                index,
                AdaptiveWindowStage.BASE
                if base is not None
                else AdaptiveWindowStage.LOCAL,
                start,
                start + plan.probe_sample_count,
                () if base is None else base.selection_reasons,
                tuple(
                    sorted(
                        selected_by_start.get(start, ()),
                        key=lambda item: str(item.digest),
                    )
                )
                if base is None
                else (),
            )
        )
    return StarlinkAdaptiveRefinementSelectionV0_1(
        SchemaRef(StarlinkAdaptiveRefinementSelectionV0_1.SCHEMA_ID, V0_1),
        segment_sample_count,
        plan,
        pattern_refs,
        base_windows,
        tuple(exact),
        (
            "candidate-evidence-not-calibrated-detection",
            "base-sentinels-span-dwell-but-do-not-cover-every-sample",
            "power-seeds-are-pattern-blind",
            "local-follow-up-uses-equal-quota-for-qin-and-surrogates",
            "all-patterns-search-the-union-of-selected-local-windows",
            "time-look-elsewhere-calibration-required",
        ),
    )
