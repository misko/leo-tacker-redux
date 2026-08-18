from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.analysis.dataset.starlink_adaptive_calibration import (
    evaluate_frozen_adaptive_calibration_inputs_v0_1,
    evaluate_locked_adaptive_calibration_v0_1,
    fit_adaptive_calibration_v0_1,
    validate_adaptive_calibration_v0_1,
)
from leo_flow.analysis.qam_goodness import qam_goodness_v0_2
from leo_flow.contracts.core import V0_1, Digest, RadioId, ReceiverChainId, SchemaRef
from leo_flow.contracts.starlink_adaptive_calibration import (
    AdaptiveCalibrationDwellV0_1,
    AdaptiveCalibrationLabel,
    AdaptiveCalibrationPlanV0_1,
    AdaptiveCalibrationSplit,
    AdaptivePatternDwellEvidenceV0_1,
    AdaptivePatternRole,
    AdaptiveReceiverPatternEvidenceV0_1,
)
from leo_flow.contracts.starlink_adaptive_calibration_input import (
    AssembledAdaptiveCalibrationInputV0_1,
)


def _digest(label: str) -> Digest:
    return Digest.sha256(label.encode())


def _plan(*, training_count: int = 10) -> AdaptiveCalibrationPlanV0_1:
    return AdaptiveCalibrationPlanV0_1(
        schema=SchemaRef(AdaptiveCalibrationPlanV0_1.SCHEMA_ID, V0_1),
        plan_id="sladcal_fixture",
        cell_identity_digest=_digest("cell"),
        pattern_count=3,
        train_manifest_digest=_digest("train-manifest"),
        validation_manifest_digest=_digest("validation-manifest"),
        locked_test_manifest_digest=_digest("locked-manifest"),
        training_null_dwell_count=training_count,
        validation_null_dwell_count=10,
        validation_positive_dwell_count=10,
        locked_test_null_dwell_count=10,
        locked_test_positive_dwell_count=10,
        target_family_wise_false_alarm_rate=0.5,
        minimum_positive_detection_probability=0.7,
        confidence_level=0.8,
        minimum_temporal_maximum=0.3,
        minimum_qam_goodness_per_receiver=0.7,
        minimum_coherent_qam_receiver_count=2,
    )


def _receiver(
    radio: str,
    receiver: str,
    score: float,
    *,
    temporal: float = 0.0,
    qam: float = 0.0,
) -> AdaptiveReceiverPatternEvidenceV0_1:
    return AdaptiveReceiverPatternEvidenceV0_1(
        RadioId(radio),
        ReceiverChainId(receiver),
        score,
        int(score > 0),
        temporal,
        qam,
        6 if qam > 0 else 0,
    )


def _pattern(
    index: int,
    scores: tuple[float, float],
    *,
    temporal: float = 0.0,
    qam: tuple[float, float] = (0.0, 0.0),
    coherent: bool = False,
) -> AdaptivePatternDwellEvidenceV0_1:
    return AdaptivePatternDwellEvidenceV0_1(
        index,
        AdaptivePatternRole.QIN if index == 0 else AdaptivePatternRole.SURROGATE,
        (
            _receiver("radio_a", "rx_a", scores[0], temporal=temporal, qam=qam[0]),
            _receiver("radio_b", "rx_b", scores[1], temporal=temporal, qam=qam[1]),
        ),
        coherent,
    )


def _dwell(
    split: AdaptiveCalibrationSplit,
    label: AdaptiveCalibrationLabel,
    index: int,
    *,
    qin: float,
    surrogate_a: float = 0.0,
    surrogate_b: float = 0.0,
    positive_qam: tuple[float, float] | None = None,
    member: Digest | None = None,
) -> AdaptiveCalibrationDwellV0_1:
    qam = positive_qam or (0.0, 0.0)
    temporal = 0.5 if positive_qam else 0.0
    return AdaptiveCalibrationDwellV0_1(
        f"{split.value}-{label.value}-{index}",
        member or _digest(f"{split.value}-{label.value}-{index}"),
        _digest(f"group-{split.value}-{label.value}-{index}"),
        split,
        label,
        _digest("cell"),
        (
            _pattern(
                0,
                (qin, qin * 0.9),
                temporal=temporal,
                qam=qam,
                coherent=positive_qam is not None,
            ),
            _pattern(1, (surrogate_a, surrogate_a * 0.9)),
            _pattern(2, (surrogate_b, surrogate_b * 0.9)),
        ),
    )


def _training() -> tuple[AdaptiveCalibrationDwellV0_1, ...]:
    return tuple(
        _dwell(
            AdaptiveCalibrationSplit.TRAIN,
            AdaptiveCalibrationLabel.NULL,
            index,
            qin=(index + 1) / 100,
        )
        for index in range(10)
    )


def _held_out(
    split: AdaptiveCalibrationSplit,
    *,
    positive_qam: tuple[float, float] = (0.8, 0.8),
) -> tuple[AdaptiveCalibrationDwellV0_1, ...]:
    nulls = tuple(
        _dwell(split, AdaptiveCalibrationLabel.NULL, index, qin=0.0)
        for index in range(10)
    )
    positives = tuple(
        _dwell(
            split,
            AdaptiveCalibrationLabel.POSITIVE,
            index,
            qin=0.4,
            surrogate_a=0.02,
            surrogate_b=0.03,
            positive_qam=positive_qam,
        )
        for index in range(10)
    )
    return (*nulls, *positives)


def _assembled(
    dwell: AdaptiveCalibrationDwellV0_1,
) -> AssembledAdaptiveCalibrationInputV0_1:
    manifest = {
        AdaptiveCalibrationSplit.TRAIN: _digest("train-manifest"),
        AdaptiveCalibrationSplit.VALIDATION: _digest("validation-manifest"),
        AdaptiveCalibrationSplit.LOCKED_TEST: _digest("locked-manifest"),
    }[dwell.split]
    return AssembledAdaptiveCalibrationInputV0_1(
        SchemaRef(AssembledAdaptiveCalibrationInputV0_1.SCHEMA_ID, V0_1),
        _digest(f"assembly-{dwell.dwell_id}"),
        manifest,
        _digest(f"response-{dwell.dwell_id}"),
        _digest(f"qam-{dwell.dwell_id}"),
        _digest("one-search-identity"),
        (_digest("qin"), _digest("surrogate-a"), _digest("surrogate-b")),
        dwell,
        ("declared-time-windows", "coarse-cfo", "residual-cfo", "epoch"),
        "none",
        True,
    )


def _fit(plan: AdaptiveCalibrationPlanV0_1 | None = None):
    selected = plan or _plan()
    return fit_adaptive_calibration_v0_1(
        selected,
        train_manifest_digest=selected.train_manifest_digest,
        training_null_dwells=_training(),
    )


def test_threshold_is_train_only_and_validation_labels_cannot_leak() -> None:
    plan = _plan()
    fit = _fit(plan)
    assert fit.threshold == 0.06
    assert fit.threshold_descending_rank == 5
    assert fit.minimum_resolvable_family_wise_far == 0.1
    assert fit.candidate_only

    changed_validation = tuple(
        replace(item, label=AdaptiveCalibrationLabel.NULL)
        for item in _held_out(AdaptiveCalibrationSplit.VALIDATION)
    )
    assert _fit(plan) == fit
    with pytest.raises(ValueError, match="dwell count"):
        validate_adaptive_calibration_v0_1(
            plan,
            fit,
            validation_manifest_digest=plan.validation_manifest_digest,
            validation_dwells=changed_validation,
        )
    with pytest.raises(ValueError, match="frozen stratum"):
        fit_adaptive_calibration_v0_1(
            plan,
            train_manifest_digest=plan.train_manifest_digest,
            training_null_dwells=(
                replace(_training()[0], label=AdaptiveCalibrationLabel.POSITIVE),
                *_training()[1:],
            ),
        )


def test_family_wise_order_statistic_includes_surrogate_and_receiver_look_elsewhere() -> (
    None
):
    plan = _plan()
    baseline = _fit(plan)
    training = list(_training())
    training[0] = _dwell(
        AdaptiveCalibrationSplit.TRAIN,
        AdaptiveCalibrationLabel.NULL,
        0,
        qin=0.01,
        surrogate_a=0.99,
    )
    fitted = fit_adaptive_calibration_v0_1(
        plan,
        train_manifest_digest=plan.train_manifest_digest,
        training_null_dwells=tuple(training),
    )
    assert fitted.threshold > baseline.threshold
    assert fitted.threshold == 0.07


def test_zero_candidate_noise_dwells_are_retained_and_not_conditioned_away() -> None:
    plan = _plan()
    fit = _fit(plan)
    validation = validate_adaptive_calibration_v0_1(
        plan,
        fit,
        validation_manifest_digest=plan.validation_manifest_digest,
        validation_dwells=_held_out(AdaptiveCalibrationSplit.VALIDATION),
    )
    assert validation.null_dwell_count == 10
    assert validation.null_family_wise_exceedance_count == 0
    assert validation.accepted
    assert all(
        receiver.candidate_count == 0 and receiver.whole_search_maximum == 0
        for dwell in _held_out(AdaptiveCalibrationSplit.VALIDATION)[:10]
        for pattern in dwell.patterns
        for receiver in pattern.receiver_evidence
    )


def test_split_overlap_and_insufficient_null_resolution_fail_closed() -> None:
    insufficient = _plan(training_count=1)
    one = (_training()[0],)
    with pytest.raises(ValueError, match="resolve"):
        fit_adaptive_calibration_v0_1(
            insufficient,
            train_manifest_digest=insufficient.train_manifest_digest,
            training_null_dwells=one,
        )

    plan = _plan()
    fit = _fit(plan)
    validation_dwells = list(_held_out(AdaptiveCalibrationSplit.VALIDATION))
    validation_dwells[0] = replace(
        validation_dwells[0], member_digest=fit.training_member_digests[0]
    )
    with pytest.raises(ValueError, match="overlaps"):
        validate_adaptive_calibration_v0_1(
            plan,
            fit,
            validation_manifest_digest=plan.validation_manifest_digest,
            validation_dwells=tuple(validation_dwells),
        )

    validation_dwells = list(_held_out(AdaptiveCalibrationSplit.VALIDATION))
    validation_dwells[0] = replace(
        validation_dwells[0], group_digest=fit.training_group_digests[0]
    )
    with pytest.raises(ValueError, match="group overlaps"):
        validate_adaptive_calibration_v0_1(
            plan,
            fit,
            validation_manifest_digest=plan.validation_manifest_digest,
            validation_dwells=tuple(validation_dwells),
        )


def test_null_fit_is_invariant_to_dwell_receiver_labels_and_surrogate_order() -> None:
    plan = _plan()
    baseline = _fit(plan)
    permuted = []
    for dwell in reversed(_training()):
        patterns = []
        for pattern in dwell.patterns:
            receivers = pattern.receiver_evidence
            relabeled = (
                replace(
                    receivers[1],
                    radio_id=RadioId("radio_a"),
                    receiver_chain_id=ReceiverChainId("rx_a"),
                ),
                replace(
                    receivers[0],
                    radio_id=RadioId("radio_b"),
                    receiver_chain_id=ReceiverChainId("rx_b"),
                ),
            )
            patterns.append(replace(pattern, receiver_evidence=relabeled))
        patterns[1], patterns[2] = (
            replace(patterns[2], pattern_index=1),
            replace(patterns[1], pattern_index=2),
        )
        permuted.append(replace(dwell, patterns=tuple(patterns)))
    assert (
        fit_adaptive_calibration_v0_1(
            plan,
            train_manifest_digest=plan.train_manifest_digest,
            training_null_dwells=permuted,
        ).threshold
        == baseline.threshold
    )


def test_retro_dual_rx_qam_positive_passes_validation_and_locked_gates() -> None:
    fixture = json.loads(
        Path(
            "tests/recording_analysis/fixtures/retro_qam_2026_08_17_v1.json"
        ).read_text()
    )
    rx = fixture["historical_conditioned_expectations"]
    goodness = tuple(
        qam_goodness_v0_2(item["hard_symbol_accuracy"], item["rms_evm"]) for item in rx
    )
    assert min(goodness) > 0.7
    plan = _plan()
    fit = _fit(plan)
    validation = validate_adaptive_calibration_v0_1(
        plan,
        fit,
        validation_manifest_digest=plan.validation_manifest_digest,
        validation_dwells=_held_out(
            AdaptiveCalibrationSplit.VALIDATION, positive_qam=goodness
        ),
    )
    locked = evaluate_locked_adaptive_calibration_v0_1(
        plan,
        fit,
        validation,
        locked_test_manifest_digest=plan.locked_test_manifest_digest,
        locked_test_dwells=_held_out(
            AdaptiveCalibrationSplit.LOCKED_TEST, positive_qam=goodness
        ),
    )
    assert locked.validation.threshold == fit.threshold
    assert locked.locked_test.threshold == fit.threshold
    assert locked.calibrated_decision_eligible
    assert not locked.candidate_only
    assert "binomial-confidence-bounds-not-p-values" in locked.warnings


def test_failed_locked_gate_remains_candidate_only_with_counts_and_wilson_bounds() -> (
    None
):
    plan = _plan()
    fit = _fit(plan)
    validation = validate_adaptive_calibration_v0_1(
        plan,
        fit,
        validation_manifest_digest=plan.validation_manifest_digest,
        validation_dwells=_held_out(AdaptiveCalibrationSplit.VALIDATION),
    )
    locked_dwells = list(_held_out(AdaptiveCalibrationSplit.LOCKED_TEST))
    for index in range(10, 20):
        target = locked_dwells[index].patterns[0]
        receivers = target.receiver_evidence
        target = replace(
            target,
            receiver_evidence=(
                receivers[0],
                replace(receivers[1], temporal_maximum=0.1),
            ),
        )
        locked_dwells[index] = replace(
            locked_dwells[index],
            patterns=(target, *locked_dwells[index].patterns[1:]),
        )
    locked = evaluate_locked_adaptive_calibration_v0_1(
        plan,
        fit,
        validation,
        locked_test_manifest_digest=plan.locked_test_manifest_digest,
        locked_test_dwells=locked_dwells,
    )
    assert locked.locked_test.positive_detection_count == 0
    assert locked.locked_test.positive_dwell_count == 10
    assert locked.locked_test.positive_detection_probability_lower_bound == 0
    assert not locked.locked_test.accepted
    assert not locked.calibrated_decision_eligible
    assert locked.candidate_only


def test_frozen_assembled_inputs_run_train_validation_and_locked_test() -> None:
    plan = _plan()
    training = list(_training())
    training[0] = _dwell(
        AdaptiveCalibrationSplit.TRAIN,
        AdaptiveCalibrationLabel.NULL,
        0,
        qin=0.01,
        surrogate_a=0.99,
    )
    fit, validation, locked = evaluate_frozen_adaptive_calibration_inputs_v0_1(
        plan,
        training_inputs=tuple(_assembled(item) for item in training),
        validation_inputs=tuple(
            _assembled(item) for item in _held_out(AdaptiveCalibrationSplit.VALIDATION)
        ),
        locked_test_inputs=tuple(
            _assembled(item) for item in _held_out(AdaptiveCalibrationSplit.LOCKED_TEST)
        ),
    )

    assert fit.threshold == 0.07
    assert fit.candidate_only
    assert validation.null_dwell_count == 10
    assert validation.null_family_wise_exceedance_count == 0
    assert locked.validation == validation
    assert locked.locked_test.null_dwell_count == 10
    assert locked.calibrated_decision_eligible
    assert not locked.candidate_only


@pytest.mark.parametrize(
    ("change", "message"),
    (
        (
            {"split_manifest_digest": _digest("wrong-manifest")},
            "frozen split",
        ),
        (
            {"search_identity_digest": _digest("different-search")},
            "search identity",
        ),
        (
            {
                "pattern_template_digests": (
                    _digest("qin"),
                    _digest("surrogate-a"),
                    _digest("different-surrogate"),
                )
            },
            "pattern bank",
        ),
    ),
)
def test_frozen_assembled_inputs_reject_changed_closure(change, message) -> None:
    plan = _plan()
    validation = tuple(
        _assembled(item) for item in _held_out(AdaptiveCalibrationSplit.VALIDATION)
    )
    validation = (replace(validation[0], **change), *validation[1:])
    with pytest.raises(ValueError, match=message):
        evaluate_frozen_adaptive_calibration_inputs_v0_1(
            plan,
            training_inputs=tuple(_assembled(item) for item in _training()),
            validation_inputs=validation,
            locked_test_inputs=tuple(
                _assembled(item)
                for item in _held_out(AdaptiveCalibrationSplit.LOCKED_TEST)
            ),
        )


def test_frozen_qam_gated_run_rejects_member_without_qam_input() -> None:
    plan = _plan()
    training = tuple(_assembled(item) for item in _training())
    training = (replace(training[0], qam_bundle_digest=None), *training[1:])
    with pytest.raises(ValueError, match="requires assembled QAM"):
        evaluate_frozen_adaptive_calibration_inputs_v0_1(
            plan,
            training_inputs=training,
            validation_inputs=tuple(
                _assembled(item)
                for item in _held_out(AdaptiveCalibrationSplit.VALIDATION)
            ),
            locked_test_inputs=tuple(
                _assembled(item)
                for item in _held_out(AdaptiveCalibrationSplit.LOCKED_TEST)
            ),
        )
