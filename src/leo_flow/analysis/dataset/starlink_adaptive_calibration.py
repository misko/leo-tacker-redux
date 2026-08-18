"""Pure staged calibration for adaptive Starlink/QAM dwell evidence."""

from __future__ import annotations

import math
from collections.abc import Sequence

from leo_flow.analysis.recording.starlink_calibration import (
    one_sided_wilson_upper_bound,
)
from leo_flow.analysis.recording.starlink_suite_calibration import (
    one_sided_wilson_lower_bound,
)
from leo_flow.contracts.core import V0_1, Digest, SchemaRef, canonical_digest
from leo_flow.contracts.starlink_adaptive_calibration import (
    AdaptiveCalibrationDwellV0_1,
    AdaptiveCalibrationLabel,
    AdaptiveCalibrationPlanV0_1,
    AdaptiveCalibrationSplit,
    AdaptiveCalibrationSplitEvidenceV0_1,
    AdaptivePatternDwellEvidenceV0_1,
    FittedAdaptiveCalibrationV0_1,
    LockedAdaptiveCalibrationEvidenceV0_1,
)
from leo_flow.contracts.starlink_adaptive_calibration_input import (
    AssembledAdaptiveCalibrationInputV0_1,
)


def evaluate_frozen_adaptive_calibration_inputs_v0_1(
    plan: AdaptiveCalibrationPlanV0_1,
    *,
    training_inputs: Sequence[AssembledAdaptiveCalibrationInputV0_1],
    validation_inputs: Sequence[AssembledAdaptiveCalibrationInputV0_1],
    locked_test_inputs: Sequence[AssembledAdaptiveCalibrationInputV0_1],
) -> tuple[
    FittedAdaptiveCalibrationV0_1,
    AdaptiveCalibrationSplitEvidenceV0_1,
    LockedAdaptiveCalibrationEvidenceV0_1,
]:
    """Run one frozen calibration cell from durable assembled evidence."""

    inputs_by_split = (
        (
            AdaptiveCalibrationSplit.TRAIN,
            plan.train_manifest_digest,
            tuple(training_inputs),
        ),
        (
            AdaptiveCalibrationSplit.VALIDATION,
            plan.validation_manifest_digest,
            tuple(validation_inputs),
        ),
        (
            AdaptiveCalibrationSplit.LOCKED_TEST,
            plan.locked_test_manifest_digest,
            tuple(locked_test_inputs),
        ),
    )
    all_inputs = tuple(
        item for _, _, split_inputs in inputs_by_split for item in split_inputs
    )
    _verify_assembled_calibration_inputs(plan, inputs_by_split, all_inputs)

    fit = fit_adaptive_calibration_v0_1(
        plan,
        train_manifest_digest=plan.train_manifest_digest,
        training_null_dwells=tuple(item.dwell for item in inputs_by_split[0][2]),
    )
    validation = validate_adaptive_calibration_v0_1(
        plan,
        fit,
        validation_manifest_digest=plan.validation_manifest_digest,
        validation_dwells=tuple(item.dwell for item in inputs_by_split[1][2]),
    )
    locked = evaluate_locked_adaptive_calibration_v0_1(
        plan,
        fit,
        validation,
        locked_test_manifest_digest=plan.locked_test_manifest_digest,
        locked_test_dwells=tuple(item.dwell for item in inputs_by_split[2][2]),
    )
    return fit, validation, locked


def _verify_assembled_calibration_inputs(
    plan: AdaptiveCalibrationPlanV0_1,
    inputs_by_split: Sequence[
        tuple[
            AdaptiveCalibrationSplit,
            Digest,
            Sequence[AssembledAdaptiveCalibrationInputV0_1],
        ]
    ],
    all_inputs: Sequence[AssembledAdaptiveCalibrationInputV0_1],
) -> None:
    if not all_inputs:
        raise ValueError("frozen calibration has no assembled inputs")
    for split, manifest_digest, split_inputs in inputs_by_split:
        for item in split_inputs:
            if (
                item.split_manifest_digest != manifest_digest
                or item.dwell.split is not split
                or item.dwell.cell_identity_digest != plan.cell_identity_digest
                or len(item.dwell.patterns) != plan.pattern_count
            ):
                raise ValueError("assembled input differs from its frozen split")
            if (
                plan.minimum_coherent_qam_receiver_count > 0
                and item.qam_bundle_digest is None
            ):
                raise ValueError("QAM-gated calibration requires assembled QAM input")
    if len({item.search_identity_digest for item in all_inputs}) != 1:
        raise ValueError("assembled inputs do not share one frozen search identity")
    if len({item.pattern_template_digests for item in all_inputs}) != 1:
        raise ValueError("assembled inputs do not share one frozen pattern bank")
    if len({item.assembly_spec_digest for item in all_inputs}) != len(all_inputs):
        raise ValueError("assembled calibration member spec is reused")


def fit_adaptive_calibration_v0_1(
    plan: AdaptiveCalibrationPlanV0_1,
    *,
    train_manifest_digest: Digest,
    training_null_dwells: Sequence[AdaptiveCalibrationDwellV0_1],
) -> FittedAdaptiveCalibrationV0_1:
    """Fit only on frozen train-null, per-dwell family-wise maxima."""

    dwells = _locked_dwells(
        plan,
        training_null_dwells,
        split=AdaptiveCalibrationSplit.TRAIN,
        label=AdaptiveCalibrationLabel.NULL,
        expected_count=plan.training_null_dwell_count,
        manifest_digest=train_manifest_digest,
        expected_manifest_digest=plan.train_manifest_digest,
    )
    maxima = tuple(_family_wise_maximum(item) for item in dwells)
    allowed = math.floor(len(maxima) * plan.target_family_wise_false_alarm_rate)
    if allowed <= 0:
        raise ValueError("training split cannot resolve its declared FAR tail")
    threshold = sorted(maxima, reverse=True)[allowed - 1]
    exceedances = sum(value > threshold for value in maxima)
    identity = canonical_digest(
        {
            "plan_digest": plan.digest,
            "train_manifest_digest": train_manifest_digest,
            "threshold": threshold,
        }
    ).value
    return FittedAdaptiveCalibrationV0_1(
        SchemaRef(FittedAdaptiveCalibrationV0_1.SCHEMA_ID, V0_1),
        f"sladfit_{identity[:32]}",
        plan.digest,
        train_manifest_digest,
        threshold,
        allowed,
        1 / len(maxima),
        len(maxima),
        exceedances,
        tuple(sorted((item.member_digest for item in dwells), key=str)),
        tuple(sorted((item.group_digest for item in dwells), key=str)),
        True,
    )


def validate_adaptive_calibration_v0_1(
    plan: AdaptiveCalibrationPlanV0_1,
    fit: FittedAdaptiveCalibrationV0_1,
    *,
    validation_manifest_digest: Digest,
    validation_dwells: Sequence[AdaptiveCalibrationDwellV0_1],
) -> AdaptiveCalibrationSplitEvidenceV0_1:
    """Audit the frozen train threshold; never refit from validation labels."""

    _fit_matches(plan, fit)
    return _evaluate_split(
        plan,
        fit,
        AdaptiveCalibrationSplit.VALIDATION,
        validation_manifest_digest,
        plan.validation_manifest_digest,
        validation_dwells,
        plan.validation_null_dwell_count,
        plan.validation_positive_dwell_count,
        fit.training_member_digests,
        fit.training_group_digests,
    )


def evaluate_locked_adaptive_calibration_v0_1(
    plan: AdaptiveCalibrationPlanV0_1,
    fit: FittedAdaptiveCalibrationV0_1,
    validation: AdaptiveCalibrationSplitEvidenceV0_1,
    *,
    locked_test_manifest_digest: Digest,
    locked_test_dwells: Sequence[AdaptiveCalibrationDwellV0_1],
) -> LockedAdaptiveCalibrationEvidenceV0_1:
    """Open the locked test only after validation passes; threshold stays frozen."""

    _fit_matches(plan, fit)
    expected_validation_far_upper = one_sided_wilson_upper_bound(
        validation.null_family_wise_exceedance_count,
        validation.null_dwell_count,
        confidence_level=plan.confidence_level,
    )
    expected_validation_pd_lower = one_sided_wilson_lower_bound(
        validation.positive_detection_count,
        validation.positive_dwell_count,
        confidence_level=plan.confidence_level,
    )
    if (
        validation.split is not AdaptiveCalibrationSplit.VALIDATION
        or validation.manifest_digest != plan.validation_manifest_digest
        or validation.threshold != fit.threshold
        or validation.null_dwell_count != plan.validation_null_dwell_count
        or validation.positive_dwell_count != plan.validation_positive_dwell_count
        or set(validation.member_digests).intersection(fit.training_member_digests)
        or set(validation.group_digests).intersection(fit.training_group_digests)
        or validation.null_far_upper_bound != expected_validation_far_upper
        or validation.positive_detection_probability_lower_bound
        != expected_validation_pd_lower
        or validation.accepted
        is not (
            validation.null_far_upper_bound <= plan.target_family_wise_false_alarm_rate
            and validation.positive_detection_probability_lower_bound
            >= plan.minimum_positive_detection_probability
        )
        or not validation.accepted
    ):
        raise ValueError("validation gate must pass before opening locked test")
    locked = _evaluate_split(
        plan,
        fit,
        AdaptiveCalibrationSplit.LOCKED_TEST,
        locked_test_manifest_digest,
        plan.locked_test_manifest_digest,
        locked_test_dwells,
        plan.locked_test_null_dwell_count,
        plan.locked_test_positive_dwell_count,
        (*fit.training_member_digests, *validation.member_digests),
        (*fit.training_group_digests, *validation.group_digests),
    )
    eligible = validation.accepted and locked.accepted
    identity = canonical_digest(
        {
            "plan": plan.digest,
            "fit": fit.digest,
            "validation": validation,
            "locked": locked,
        }
    ).value
    return LockedAdaptiveCalibrationEvidenceV0_1(
        SchemaRef(LockedAdaptiveCalibrationEvidenceV0_1.SCHEMA_ID, V0_1),
        f"sladlocked_{identity[:32]}",
        plan.digest,
        fit.digest,
        validation,
        locked,
        eligible,
        not eligible,
        (
            "whole-search-family-wise-maxima",
            "qin-and-surrogates-treated-symmetrically-under-null",
            "zero-candidate-null-dwells-retained",
            "locked-test-must-be-evaluated-once",
            "binomial-confidence-bounds-not-p-values",
        ),
    )


def _evaluate_split(
    plan: AdaptiveCalibrationPlanV0_1,
    fit: FittedAdaptiveCalibrationV0_1,
    split: AdaptiveCalibrationSplit,
    manifest_digest: Digest,
    expected_manifest_digest: Digest,
    dwells: Sequence[AdaptiveCalibrationDwellV0_1],
    expected_null_count: int,
    expected_positive_count: int,
    excluded_member_digests: Sequence[Digest],
    excluded_group_digests: Sequence[Digest],
) -> AdaptiveCalibrationSplitEvidenceV0_1:
    nulls = _locked_dwells(
        plan,
        tuple(item for item in dwells if item.label is AdaptiveCalibrationLabel.NULL),
        split=split,
        label=AdaptiveCalibrationLabel.NULL,
        expected_count=expected_null_count,
        manifest_digest=manifest_digest,
        expected_manifest_digest=expected_manifest_digest,
    )
    positives = _locked_dwells(
        plan,
        tuple(
            item for item in dwells if item.label is AdaptiveCalibrationLabel.POSITIVE
        ),
        split=split,
        label=AdaptiveCalibrationLabel.POSITIVE,
        expected_count=expected_positive_count,
        manifest_digest=manifest_digest,
        expected_manifest_digest=expected_manifest_digest,
    )
    if len(nulls) + len(positives) != len(dwells):
        raise ValueError("held-out split contains an unknown truth label")
    member_digests = tuple(item.member_digest for item in (*nulls, *positives))
    group_digests = tuple(item.group_digest for item in (*nulls, *positives))
    if len(member_digests) != len(set(member_digests)):
        raise ValueError("held-out split reuses a dwell member")
    if set(member_digests).intersection(excluded_member_digests):
        raise ValueError("calibration member overlaps an earlier frozen split")
    if set(group_digests).intersection(excluded_group_digests):
        raise ValueError("calibration group overlaps an earlier frozen split")
    false_alarms = sum(_dwell_fires(plan, item, fit.threshold) for item in nulls)
    detections = sum(_target_fires(plan, item, fit.threshold) for item in positives)
    far_upper = one_sided_wilson_upper_bound(
        false_alarms,
        len(nulls),
        confidence_level=plan.confidence_level,
    )
    pd_lower = one_sided_wilson_lower_bound(
        detections,
        len(positives),
        confidence_level=plan.confidence_level,
    )
    accepted = (
        far_upper <= plan.target_family_wise_false_alarm_rate
        and pd_lower >= plan.minimum_positive_detection_probability
    )
    return AdaptiveCalibrationSplitEvidenceV0_1(
        split,
        manifest_digest,
        fit.threshold,
        len(nulls),
        false_alarms,
        far_upper,
        len(positives),
        detections,
        pd_lower,
        tuple(sorted(member_digests, key=str)),
        tuple(sorted(group_digests, key=str)),
        accepted,
    )


def _locked_dwells(
    plan: AdaptiveCalibrationPlanV0_1,
    values: Sequence[AdaptiveCalibrationDwellV0_1],
    *,
    split: AdaptiveCalibrationSplit,
    label: AdaptiveCalibrationLabel,
    expected_count: int,
    manifest_digest: Digest,
    expected_manifest_digest: Digest,
) -> tuple[AdaptiveCalibrationDwellV0_1, ...]:
    dwells = tuple(values)
    if manifest_digest != expected_manifest_digest:
        raise ValueError("calibration split manifest differs from the frozen plan")
    if len(dwells) != expected_count:
        raise ValueError("calibration dwell count differs from the frozen plan")
    identities = tuple(item.dwell_id for item in dwells)
    members = tuple(item.member_digest for item in dwells)
    groups = tuple(item.group_digest for item in dwells)
    if (
        len(identities) != len(set(identities))
        or len(members) != len(set(members))
        or len(groups) != len(set(groups))
    ):
        raise ValueError("calibration split contains duplicate dwell members")
    for item in dwells:
        if (
            item.split is not split
            or item.label is not label
            or item.cell_identity_digest != plan.cell_identity_digest
            or len(item.patterns) != plan.pattern_count
        ):
            raise ValueError("calibration dwell differs from its frozen stratum")
    return dwells


def _fit_matches(
    plan: AdaptiveCalibrationPlanV0_1, fit: FittedAdaptiveCalibrationV0_1
) -> None:
    if (
        fit.plan_digest != plan.digest
        or fit.train_manifest_digest != plan.train_manifest_digest
        or not fit.candidate_only
    ):
        raise ValueError("adaptive fit differs from the frozen plan")


def _family_wise_maximum(dwell: AdaptiveCalibrationDwellV0_1) -> float:
    """Maximum over time/CFO/epoch is input; finish RX/pattern maxima here."""

    return max(pattern.whole_search_maximum for pattern in dwell.patterns)


def _pattern_passes_evidence_gates(
    plan: AdaptiveCalibrationPlanV0_1, pattern: AdaptivePatternDwellEvidenceV0_1
) -> bool:
    if plan.minimum_coherent_qam_receiver_count == 0:
        return any(
            item.temporal_maximum >= plan.minimum_temporal_maximum
            for item in pattern.receiver_evidence
        )
    qualified = sum(
        item.qam_complete_frame_count > 0
        and item.qam_goodness >= plan.minimum_qam_goodness_per_receiver
        and item.temporal_maximum >= plan.minimum_temporal_maximum
        for item in pattern.receiver_evidence
    )
    return (
        pattern.dual_rx_coherent_qam
        and qualified >= plan.minimum_coherent_qam_receiver_count
    )


def _dwell_fires(
    plan: AdaptiveCalibrationPlanV0_1,
    dwell: AdaptiveCalibrationDwellV0_1,
    threshold: float,
) -> bool:
    return any(
        pattern.whole_search_maximum > threshold
        and _pattern_passes_evidence_gates(plan, pattern)
        for pattern in dwell.patterns
    )


def _target_fires(
    plan: AdaptiveCalibrationPlanV0_1,
    dwell: AdaptiveCalibrationDwellV0_1,
    threshold: float,
) -> bool:
    target = dwell.patterns[0]
    return target.whole_search_maximum > threshold and _pattern_passes_evidence_gates(
        plan, target
    )
