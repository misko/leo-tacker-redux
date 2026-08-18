from __future__ import annotations

import math
from dataclasses import replace

import pytest

from leo_flow.analysis.recording.starlink_receiver_agnostic_cfo import (
    receiver_agnostic_cfo_search_v0_6,
)
from leo_flow.contracts.core import Digest
from leo_flow.contracts.starlink_adaptive_calibration import AdaptivePatternRole
from leo_flow.contracts.starlink_receiver_agnostic_cfo import (
    ReceiverAgnosticCfoPatternV0_6,
    ReceiverAgnosticCfoSearchPlanV0_6,
)


def _patterns(count: int = 5):
    return tuple(
        ReceiverAgnosticCfoPatternV0_6(
            index,
            AdaptivePatternRole.QIN
            if index == 0
            else AdaptivePatternRole.SURROGATE,
            Digest.sha256(f"pattern-{index}".encode()),
        )
        for index in range(count)
    )


class _Peaks:
    def __init__(self, peaks):
        self.peaks = peaks
        self.calls = []

    def score(self, pattern, epoch_sample, cfo_hz):
        self.calls.append((pattern.template_digest, epoch_sample, cfo_hz))
        target_epoch, target_cfo = self.peaks[pattern.template_digest]
        epoch_error = min(
            abs(epoch_sample - target_epoch), 64 - abs(epoch_sample - target_epoch)
        )
        return math.exp(-((epoch_error / 1.5) ** 2 + ((cfo_hz - target_cfo) / 8_000) ** 2))


def _compact_plan(**changes):
    values = {
        "coarse_cfo_step_hz": 350_000.0,
        "local_cfo_radius_hz": 350_000.0,
        "local_cfo_step_hz": 5_000.0,
        "basins_per_pattern": 1,
        "basin_cfo_separation_hz": 350_000.0,
    }
    values.update(changes)
    return replace(ReceiverAgnosticCfoSearchPlanV0_6(), **values)


@pytest.mark.parametrize("cfo_hz", (-700_000.0, 700_000.0))
def test_exact_declared_cfo_endpoints_are_searched_and_recovered(cfo_hz):
    patterns = _patterns(1)
    scorer = _Peaks({patterns[0].template_digest: (63, cfo_hz)})
    result = receiver_agnostic_cfo_search_v0_6(
        _compact_plan(), 64, patterns, scorer
    )
    assert result.plan.cfo_min_hz == -700_000.0
    assert result.plan.cfo_max_hz == 700_000.0
    assert result.winners[0].epoch_sample == 63
    assert result.winners[0].cfo_hz == cfo_hz


def test_every_epoch_residue_is_reachable_by_local_refinement():
    patterns = _patterns(1)
    for residue in range(64):
        cfo = (-700_000.0, -350_000.0, 0.0, 350_000.0, 700_000.0)[residue % 5]
        result = receiver_agnostic_cfo_search_v0_6(
            _compact_plan(),
            64,
            patterns,
            _Peaks({patterns[0].template_digest: (residue, cfo)}),
        )
        assert (result.winners[0].epoch_sample, result.winners[0].cfo_hz) == (
            residue,
            cfo,
        )


def test_alias_basin_and_pattern_union_are_scored_symmetrically():
    patterns = _patterns(3)
    peaks = {
        patterns[0].template_digest: (9, 620_000.0),
        patterns[1].template_digest: (41, -615_000.0),
        patterns[2].template_digest: (27, 85_000.0),
    }
    scorer = _Peaks(peaks)
    result = receiver_agnostic_cfo_search_v0_6(
        _compact_plan(basins_per_pattern=2), 64, patterns, scorer
    )
    assert [winner.cfo_hz for winner in result.winners] == pytest.approx(
        [620_000.0, -615_000.0, 85_000.0]
    )
    assert all(len(cell.pattern_scores) == 3 for cell in result.cells)
    assert result.pattern_evaluation_count == len(result.cells) * 3
    assert result.look_elsewhere_hypothesis_count == result.pattern_evaluation_count
    assert len(scorer.calls) == result.pattern_evaluation_count
    assert any(len(cell.selected_by_pattern_indices) > 1 for cell in result.cells)


class _Flat:
    def __init__(self, values):
        self.values = values

    def score(self, pattern, epoch_sample, cfo_hz):
        del epoch_sample, cfo_hz
        return self.values[pattern.template_digest]


@pytest.mark.parametrize("qin_score", (0.0, 0.2))
def test_noise_and_wrong_pattern_never_create_a_verdict(qin_score):
    patterns = _patterns(2)
    result = receiver_agnostic_cfo_search_v0_6(
        _compact_plan(),
        64,
        patterns,
        _Flat(
            {
                patterns[0].template_digest: qin_score,
                patterns[1].template_digest: 0.9,
            }
        ),
    )
    assert result.candidates_only
    assert result.calibrated_detection_count is None
    assert "no-detection-threshold-or-calibration-claim" in result.disclosures


def test_surrogate_label_permutation_is_equivariant():
    left_patterns = _patterns(3)
    peaks = {
        left_patterns[0].template_digest: (5, 100_000.0),
        left_patterns[1].template_digest: (17, -200_000.0),
        left_patterns[2].template_digest: (29, 300_000.0),
    }
    right_patterns = (
        left_patterns[0],
        replace(left_patterns[2], pattern_index=1),
        replace(left_patterns[1], pattern_index=2),
    )
    left = receiver_agnostic_cfo_search_v0_6(
        _compact_plan(), 64, left_patterns, _Peaks(peaks)
    )
    right = receiver_agnostic_cfo_search_v0_6(
        _compact_plan(), 64, right_patterns, _Peaks(peaks)
    )
    left_by_digest = {
        pattern.template_digest: (winner.epoch_sample, winner.cfo_hz, winner.score)
        for pattern, winner in zip(left.patterns, left.winners, strict=True)
    }
    right_by_digest = {
        pattern.template_digest: (winner.epoch_sample, winner.cfo_hz, winner.score)
        for pattern, winner in zip(right.patterns, right.winners, strict=True)
    }
    assert right_by_digest == left_by_digest
    assert {(cell.epoch_sample, cell.cfo_hz) for cell in right.cells} == {
        (cell.epoch_sample, cell.cfo_hz) for cell in left.cells
    }


@pytest.mark.parametrize(
    ("canary", "epoch", "cfo_hz"),
    (
        ("j1-early-rx-c", 11, 450_423.9939616095),
        ("j1-late-rx-c", 37, 373_552.47880750697),
        ("j1-early-rx-d", 19, -161_334.286223267),
        ("j1-late-rx-d", 43, -237_920.9309141801),
        ("retro-rx-0", 15, 364_150.8476787003),
        ("retro-rx-1", 15, -194_343.8743595247),
    ),
)
def test_frozen_conditioned_j1_and_retro_numerics_are_coverage_canaries_only(
    canary, epoch, cfo_hz
):
    del canary
    patterns = _patterns(1)
    result = receiver_agnostic_cfo_search_v0_6(
        _compact_plan(),
        64,
        patterns,
        _Peaks({patterns[0].template_digest: (epoch, cfo_hz)}),
    )
    assert result.winners[0].cfo_hz == pytest.approx(cfo_hz, abs=2_500.0)
    assert result.calibrated_detection_count is None
    assert "retro-and-j1-are-conditioned-numerical-canaries-only" in result.disclosures


def test_cost_is_exact_bounded_and_compared_to_predecessor_domains():
    patterns = _patterns(5)
    peaks = {
        pattern.template_digest: (pattern.pattern_index * 9, -600_000 + pattern.pattern_index * 300_000)
        for pattern in patterns
    }
    plan = _compact_plan(basins_per_pattern=2)
    result = receiver_agnostic_cfo_search_v0_6(plan, 64, patterns, _Peaks(peaks))
    assert result.coarse_cell_count == 45
    assert result.local_cell_count <= plan.maximum_local_cells
    assert result.unique_cell_count <= plan.maximum_unique_cells
    assert result.pattern_evaluation_count <= plan.maximum_pattern_evaluations
    # The predecessor domains are strict subsets and miss both new endpoints.
    assert -350_000.0 > plan.cfo_min_hz and 350_000.0 < plan.cfo_max_hz
    assert -400_000.0 > plan.cfo_min_hz and 400_000.0 < plan.cfo_max_hz


def test_plan_rejects_receiver_corrections_narrow_coverage_and_unbounded_cost():
    with pytest.raises(ValueError, match="cover at least"):
        ReceiverAgnosticCfoSearchPlanV0_6(cfo_min_hz=-699_999.0)
    with pytest.raises(ValueError, match="forbidden"):
        ReceiverAgnosticCfoSearchPlanV0_6(receiver_adjustment_policy="lnb-a:+600k")
    patterns = _patterns(2)
    with pytest.raises(ValueError, match="pattern-evaluation bound"):
        receiver_agnostic_cfo_search_v0_6(
            replace(_compact_plan(), maximum_pattern_evaluations=10),
            64,
            patterns,
            _Flat({pattern.template_digest: 0.0 for pattern in patterns}),
        )
