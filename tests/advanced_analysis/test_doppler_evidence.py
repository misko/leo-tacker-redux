from __future__ import annotations

import json
from pathlib import Path

import pytest

from leo_flow.analysis.doppler_evidence import (
    associate_tle_post_blind,
    broadband_motion_support,
    comb_support,
    dedoppler_slope_bank,
    dual_receiver_consensus,
    viterbi_peel_tracks,
)

FIXTURE = Path(__file__).parent / "fixtures" / "doppler_evidence_oracle_v0_1.json"


def _blank(rows: int, columns: int, value: float = 0.0) -> list[list[float]]:
    return [[value for _ in range(columns)] for _ in range(rows)]


def test_frozen_reference_oracle_linear_slope_and_controls() -> None:
    case = json.loads(FIXTURE.read_text())["synthetic_linear_case"]
    power = _blank(case["rows"], case["columns"])
    for row in range(case["rows"]):
        power[row][case["intercept_bin"] + row] = case["signal_power"]

    evidence = dedoppler_slope_bank(power, (-1.0, 0.0, 1.0))

    assert evidence.track.slope_bins_per_row == pytest.approx(
        case["slope_bins_per_row"], abs=case["maximum_slope_error_bins_per_row"]
    )
    assert evidence.heldout_score == pytest.approx(case["expected_heldout_score"])
    assert evidence.track.stationary_improvement == pytest.approx(
        case["expected_stationary_improvement"]
    )
    assert evidence.heldout_score > evidence.opposite_slope_score
    assert all(evidence.heldout_score > score for score in evidence.shuffled_scores)


def test_stationary_path_does_not_claim_motion() -> None:
    power = _blank(10, 20)
    for row in range(10):
        power[row][8] = 4.0

    evidence = dedoppler_slope_bank(power, (-1.0, 0.0, 1.0))

    assert evidence.track.slope_bins_per_row == 0.0
    assert evidence.track.stationary_improvement == 0.0


def test_slope_selection_never_uses_final_test_rows() -> None:
    power = _blank(12, 40)
    training = tuple(range(0, 12, 3))
    validation = tuple(range(1, 12, 3))
    test = tuple(range(2, 12, 3))
    for row in training:
        power[row][5 + row] = 5.0
        power[row][30 - row] = 4.0
    for row in validation:
        power[row][5 + row] = 5.0
    for row in test:
        power[row][30 - row] = 4.0

    evidence = dedoppler_slope_bank(power, (-1.0, 0.0, 1.0))

    assert evidence.training_rows == training
    assert evidence.validation_rows == validation
    assert evidence.test_rows == test
    assert evidence.track.slope_bins_per_row == 1.0
    assert evidence.heldout_score == 0.0
    assert evidence.opposite_slope_score == 4.0


def test_viterbi_peels_two_separated_tracks() -> None:
    power = _blank(12, 40)
    expected = []
    for row in range(12):
        first = 4 + row
        second = 34 - row
        expected.append((first, second))
        power[row][first] = 9.0
        power[row][second] = 7.0

    tracks = viterbi_peel_tracks(
        power, maximum_step_bins=1, track_count=2, peel_radius_bins=0
    )

    recovered = {track.bins for track in tracks}
    assert tuple(pair[0] for pair in expected) in recovered
    assert tuple(pair[1] for pair in expected) in recovered
    assert sorted(abs(track.slope_bins_per_row) for track in tracks) == pytest.approx(
        [1, 1]
    )


def test_comb_uses_heldout_teeth_and_rejects_wrong_spacing() -> None:
    power = _blank(10, 100)
    path = tuple(40 + row for row in range(10))
    for row, center in enumerate(path):
        for tooth in range(-4, 5):
            power[row][center + tooth * 5] = 6.0 if tooth % 2 else 8.0

    evidence = comb_support(power, path, spacing_bins=5, wrong_spacing_bins=4)

    assert evidence.fit_score == pytest.approx(8.0)
    assert evidence.heldout_score == pytest.approx(6.0)
    assert evidence.wrong_spacing_score < 1.0


def test_broadband_edges_and_texture_share_translation() -> None:
    lower = tuple(20 + row for row in range(8))
    upper = tuple(44 + row for row in range(8))
    texture = []
    base = [((column * 7) % 13) / 13 for column in range(80)]
    for row in range(8):
        texture.append([0.0] * row + base[: 80 - row])

    evidence = broadband_motion_support(
        lower, upper, texture, maximum_texture_step_bins=2
    )

    assert evidence.lower_slope_bins_per_row == pytest.approx(1.0)
    assert evidence.upper_slope_bins_per_row == pytest.approx(1.0)
    assert evidence.edge_slope_difference == pytest.approx(0.0)
    assert evidence.width_mad_fraction == pytest.approx(0.0)
    assert evidence.texture_shift_bins == pytest.approx(1.0)
    assert evidence.texture_correlation > 0.99


def test_centroid_jump_does_not_fake_edge_translation() -> None:
    lower = (20, 20, 20, 20, 20, 20)
    upper = (50, 50, 50, 70, 70, 70)
    texture = [[float(column % 5) for column in range(80)] for _ in lower]

    evidence = broadband_motion_support(
        lower, upper, texture, maximum_texture_step_bins=2
    )

    assert evidence.edge_slope_difference > 3.0
    assert evidence.width_mad_fraction > 0.35
    assert evidence.texture_shift_bins == 0.0


def test_frozen_reference_oracle_dual_receiver_offset_is_nuisance() -> None:
    case = json.loads(FIXTURE.read_text())["dual_receiver_case"]

    evidence = dual_receiver_consensus(case["first_bins"], case["second_bins"])

    assert evidence.receiver_offsets_bins == pytest.approx(
        (0.0, case["expected_offset_bins"])
    )
    assert evidence.slope_difference == pytest.approx(case["expected_slope_difference"])
    assert evidence.offset_removed_rms_bins == pytest.approx(
        case["expected_offset_removed_rms_bins"]
    )
    assert evidence.path_correlation > 0.999


def test_dual_receiver_rejects_different_motion_despite_offset_fit() -> None:
    evidence = dual_receiver_consensus(
        (10, 11, 12, 13, 14, 15),
        (40, 42, 44, 46, 48, 50),
    )

    assert evidence.slope_difference == pytest.approx(1.0)
    assert evidence.offset_removed_rms_bins > 1.0


def test_tle_association_is_post_blind_and_uses_heldout_controls() -> None:
    case = json.loads(FIXTURE.read_text())["tle_case"]
    predictions = {
        "correct": case["correct_shape"],
        "wrong": case["wrong_shape"],
    }

    with pytest.raises(ValueError, match="before blind qualification"):
        associate_tle_post_blind(
            case["observed_bins"],
            predictions,
            blind_qualified=False,
            minimum_runner_up_margin_bins=1.0,
        )

    result = associate_tle_post_blind(
        case["observed_bins"],
        predictions,
        blind_qualified=True,
        minimum_runner_up_margin_bins=1.0,
    )

    assert result.name == case["expected_name"]
    assert result.offset_bins == pytest.approx(case["expected_offset_bins"])
    assert result.heldout_rms_bins == pytest.approx(case["expected_heldout_rms_bins"])
    assert result.runner_up_margin_bins > 1.0
    assert result.qualified


def test_tle_near_tie_remains_unqualified() -> None:
    observed = (10, 11, 13, 16, 20, 25, 31, 38)
    result = associate_tle_post_blind(
        observed,
        {
            "a": (0, 1, 3, 6, 10, 15, 21, 28),
            "near-tie": (0, 1, 3, 6, 10, 15, 21, 27.9),
        },
        blind_qualified=True,
        minimum_runner_up_margin_bins=0.2,
    )

    assert result.heldout_rms_bins == pytest.approx(0.0)
    assert result.runner_up_margin_bins < 0.2
    assert not result.qualified
