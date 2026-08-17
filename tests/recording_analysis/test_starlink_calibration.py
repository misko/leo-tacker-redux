from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.recording.starlink_calibration import (
    approved_calibration_v0_1,
    evaluate_calibration_cell_v0_1,
    minimum_holdout_searches,
    one_sided_wilson_upper_bound,
    plan_starlink_calibration_cell_v0_1,
)
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    RadioId,
    ReceiverChainId,
    SchemaRef,
)
from leo_flow.contracts.starlink import StarlinkEdge


def _digest(label: str) -> Digest:
    return Digest.sha256(label.encode())


def _plan():
    return plan_starlink_calibration_cell_v0_1(
        cell_id="slcalcell_radio20_rx0_ch1_lower",
        radio_id=RadioId("radio_pluto20"),
        receiver_chain_id=ReceiverChainId("rx_0"),
        hardware_profile_digest=_digest("radio20-v5-lnb-port-rx0-gain-rate"),
        tuning_identity_digest=_digest("channel1-lower-edge-tuning"),
        channel_number=1,
        edge=StarlinkEdge.LOWER,
        algorithm_digest=_digest("algorithm"),
        config_digest=_digest("config"),
        exact_template_digest=_digest("exact-template"),
        conditioned_control_template_digest=_digest("roll17-control"),
        search_identity_digest=_digest("whole-search"),
        positive_injection_plan_ref=ArtifactRef(
            "starlink-injection-grid-v0.1",
            _digest("snr-cfo-epoch-occupancy-grid"),
            SchemaRef("org.leo-flow.starlink-positive-injection-plan", V0_1),
        ),
    )


def test_default_plan_has_tail_resolution_and_heldout_confidence() -> None:
    plan = _plan()

    assert plan.training_null_search_count == 10_000
    assert plan.expected_training_tail_count == 100
    assert plan.expected_holdout_exceedance_count == 20
    assert plan.holdout_null_search_count == 4_000
    assert plan.holdout_null_search_count == minimum_holdout_searches(0.01)
    expected_exceedances = int(plan.holdout_null_search_count * plan.holdout_design_far)
    assert (
        one_sided_wilson_upper_bound(
            expected_exceedances,
            plan.holdout_null_search_count,
            confidence_level=plan.confidence_level,
        )
        <= 0.01
    )
    assert plan.radio_id == RadioId("radio_pluto20")


def test_disjoint_holdout_gate_is_required_before_calibration() -> None:
    plan = _plan()
    training = tuple(index / 10_000 for index in range(10_000))
    holdout = (0.0,) * plan.holdout_null_search_count
    evidence = evaluate_calibration_cell_v0_1(
        plan,
        corpus_digest=_digest("locked-corpus"),
        training_null_scores=training,
        holdout_null_scores=holdout,
        positive_scores_by_snr_db={-12.0: (0.5,) * 20, -6.0: (1.0,) * 20},
    )

    assert evidence.accepted is True
    assert evidence.training_threshold_exceedance_count <= 100
    assert evidence.holdout_threshold_exceedance_count == 0
    assert evidence.holdout_far_upper_bound <= 0.01
    assert evidence.positive_performance[0].detection_probability == 0.0
    assert evidence.positive_performance[1].detection_probability == 1.0

    calibration = approved_calibration_v0_1(
        plan,
        evidence,
        calibration_id="slcalibration_radio20_rx0_ch1_lower",
        null_dataset_digest=_digest("null-dataset"),
        null_split_digest=_digest("frozen-train-holdout-split"),
    )
    assert calibration.threshold == evidence.threshold
    assert calibration.null_search_count == plan.holdout_null_search_count
    assert calibration.threshold_exceedance_count == 0


def test_failed_far_bound_cannot_be_promoted() -> None:
    plan = _plan()
    evidence = evaluate_calibration_cell_v0_1(
        plan,
        corpus_digest=_digest("contaminated-corpus"),
        training_null_scores=tuple(index / 10_000 for index in range(10_000)),
        holdout_null_scores=(1.0,) * plan.holdout_null_search_count,
        positive_scores_by_snr_db={-6.0: (1.0,)},
    )
    assert evidence.accepted is False

    with pytest.raises(ValueError, match="FAR confidence gate failed"):
        approved_calibration_v0_1(
            plan,
            evidence,
            calibration_id="slcalibration_rejected",
            null_dataset_digest=_digest("null-dataset"),
            null_split_digest=_digest("split"),
        )


def test_plan_and_evidence_fail_closed_on_identity_or_count_drift() -> None:
    plan = _plan()
    with pytest.raises(ValueError, match="training null count"):
        evaluate_calibration_cell_v0_1(
            plan,
            corpus_digest=_digest("corpus"),
            training_null_scores=(0.0,),
            holdout_null_scores=(0.0,) * plan.holdout_null_search_count,
            positive_scores_by_snr_db={-6.0: (1.0,)},
        )

    evidence = evaluate_calibration_cell_v0_1(
        plan,
        corpus_digest=_digest("corpus"),
        training_null_scores=(0.0,) * plan.training_null_search_count,
        holdout_null_scores=(0.0,) * plan.holdout_null_search_count,
        positive_scores_by_snr_db={-6.0: (1.0,)},
    )
    with pytest.raises(ValueError, match="another cell plan"):
        approved_calibration_v0_1(
            replace(plan, tuning_identity_digest=_digest("different-tuning")),
            evidence,
            calibration_id="slcalibration_wrong_cell",
            null_dataset_digest=_digest("null-dataset"),
            null_split_digest=_digest("split"),
        )


@pytest.mark.parametrize(
    "count,total",
    [(-1, 10), (11, 10), (0, 0)],
)
def test_wilson_bound_rejects_invalid_counts(count: int, total: int) -> None:
    with pytest.raises(ValueError, match="outside"):
        one_sided_wilson_upper_bound(count, total)
