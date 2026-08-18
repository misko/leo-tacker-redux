from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.recording.starlink_adaptive_refinement import (
    adaptive_base_windows_v0_1,
    adaptive_refinement_selection_v0_1,
)
from leo_flow.contracts.core import ArtifactRef, Digest, SchemaRef
from leo_flow.contracts.starlink_adaptive_refinement import (
    AdaptiveWindowStage,
    StarlinkAdaptivePatternScoreV0_1,
    StarlinkAdaptiveRefinementPlanV0_1,
)
from leo_flow.contracts.starlink_surrogate_null import V0_1


def _ref(name: str) -> ArtifactRef:
    return ArtifactRef(name, Digest.sha256(name.encode()), SchemaRef("pattern", V0_1))


def _plan() -> StarlinkAdaptiveRefinementPlanV0_1:
    # Scaled geometry: 20-sample probes, sentinels every 300 samples, and
    # +/-50 samples at a 10-sample local stride.
    return StarlinkAdaptiveRefinementPlanV0_1(20, 300, 50, 10, 1, 2, 16, 64)


def test_base_windows_span_endpoints_and_merge_pattern_blind_power_seeds() -> None:
    base = adaptive_base_windows_v0_1(
        1_000,
        _plan(),
        ((0, 391, 399), (1, 690, 710)),
    )
    assert base[0].start_sample == 0
    assert base[-1].start_sample == 980
    assert {item.start_sample for item in base} == {0, 300, 385, 600, 690, 900, 980}
    power = [item for item in base if item.power_seed_ranks]
    assert [(item.start_sample, item.power_seed_ranks) for item in power] == [
        (385, (0,)),
        (690, (1,)),
    ]


def test_qin_and_surrogates_get_equal_quota_and_all_search_the_union() -> None:
    plan = _plan()
    base = adaptive_base_windows_v0_1(1_000, plan, ((0, 390, 410),))
    qin, surrogate = _ref("qin"), _ref("surrogate-0")
    scores = tuple(
        StarlinkAdaptivePatternScoreV0_1(pattern, item.start_sample, score)
        for pattern, preferred in ((qin, 300), (surrogate, 600))
        for item in base
        for score in (0.9 if item.start_sample == preferred else 0.1,)
    )
    result = adaptive_refinement_selection_v0_1(1_000, plan, base, scores)
    local = [
        item for item in result.exact_windows if item.stage is AdaptiveWindowStage.LOCAL
    ]
    qin_starts = {
        item.start_sample for item in local if qin in item.selected_by_pattern_refs
    }
    surrogate_starts = {
        item.start_sample
        for item in local
        if surrogate in item.selected_by_pattern_refs
    }
    assert qin_starts == {250, 260, 270, 280, 290, 310, 320, 330, 340, 350}
    assert surrogate_starts == {
        550,
        560,
        570,
        580,
        590,
        610,
        620,
        630,
        640,
        650,
    }
    assert result.pattern_refs == tuple(
        sorted((qin, surrogate), key=lambda x: str(x.digest))
    )
    assert "all-patterns-search-the-union-of-selected-local-windows" in result.warnings


def test_follow_up_is_invariant_to_pattern_names_and_input_order() -> None:
    plan = _plan()
    base = adaptive_base_windows_v0_1(1_000, plan, ((0, 390, 410),))
    first, second = _ref("first"), _ref("second")
    scores = tuple(
        StarlinkAdaptivePatternScoreV0_1(pattern, item.start_sample, score)
        for pattern, preferred in ((first, 300), (second, 600))
        for item in base
        for score in (0.9 if item.start_sample == preferred else 0.1,)
    )
    original = adaptive_refinement_selection_v0_1(1_000, plan, base, scores)
    swapped_scores = tuple(
        replace(item, pattern_ref=second if item.pattern_ref == first else first)
        for item in reversed(scores)
    )
    swapped = adaptive_refinement_selection_v0_1(1_000, plan, base, swapped_scores)
    assert tuple(item.start_sample for item in original.exact_windows) == tuple(
        item.start_sample for item in swapped.exact_windows
    )


def test_missing_or_duplicate_pattern_scores_fail_closed() -> None:
    plan = _plan()
    base = adaptive_base_windows_v0_1(1_000, plan, ())
    pattern = _ref("qin")
    incomplete = tuple(
        StarlinkAdaptivePatternScoreV0_1(pattern, item.start_sample, 0.1)
        for item in base[:-1]
    )
    with pytest.raises(ValueError, match="every pattern"):
        adaptive_refinement_selection_v0_1(1_000, plan, base, incomplete)
    duplicate = (
        StarlinkAdaptivePatternScoreV0_1(pattern, base[0].start_sample, 0.1),
    ) * 2
    with pytest.raises(ValueError, match="duplicated"):
        adaptive_refinement_selection_v0_1(1_000, plan, base, duplicate)


def test_declared_bounds_are_enforced_without_silent_truncation() -> None:
    plan = _plan()
    with pytest.raises(ValueError, match="power seeds"):
        adaptive_base_windows_v0_1(
            1_000,
            plan,
            ((0, 10, 20), (1, 30, 40), (2, 50, 60)),
        )
    base = adaptive_base_windows_v0_1(1_000, plan, ())
    scores = tuple(
        StarlinkAdaptivePatternScoreV0_1(pattern, item.start_sample, score)
        for pattern, preferred in ((_ref("qin"), 300), (_ref("surrogate"), 600))
        for item in base
        for score in (0.9 if item.start_sample == preferred else 0.1,)
    )
    with pytest.raises(ValueError, match="local union"):
        adaptive_refinement_selection_v0_1(
            1_000, replace(plan, maximum_exact_windows=16), base, scores
        )
