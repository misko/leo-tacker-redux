"""Deterministic, TRAIN-only threshold calibration for Starlink benchmarks.

This is benchmark support, not a production threshold fitter.  It accepts only
already-extracted FeatureSets and frozen binary membership.  Validation and
locked-test members have no representation at this boundary, preventing their
scores or labels from influencing the calibrated rule.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from leo_flow.analysis.dataset import DatasetSplit
from leo_flow.analysis.recording import ThresholdRule, encode_feature_set
from leo_flow.contracts.core import Digest, canonical_digest
from leo_flow.contracts.features import FeatureSetBundle

CALIBRATION_POLICY = "train-recording-max-balanced-accuracy-higher-threshold-v1"


@dataclass(frozen=True)
class FrozenTrainCalibrationMember:
    """One immutable TRAIN FeatureSet and its detector-independent label."""

    bundle: FeatureSetBundle
    target_present: bool
    split_group_id: str
    split: DatasetSplit = DatasetSplit.TRAIN

    def __post_init__(self) -> None:
        if self.split is not DatasetSplit.TRAIN:
            raise ValueError("threshold calibration accepts TRAIN members only")
        if not isinstance(self.target_present, bool):
            raise TypeError("threshold calibration labels must be binary booleans")
        if not self.split_group_id or any(
            character.isspace() for character in self.split_group_id
        ):
            raise ValueError("split_group_id must be a non-empty token")


def calibrate_train_thresholds(
    members: Iterable[FrozenTrainCalibrationMember],
    *,
    expected_method_ids: tuple[str, ...],
) -> ThresholdRule:
    """Fit exact methods using per-recording maxima and TRAIN labels only.

    For each method, every recording contributes exactly one value: the maximum
    score across all of its segments and windows.  The selected finite
    threshold maximizes recording-level balanced accuracy.  Equal optima choose
    the higher threshold, yielding a deterministic conservative tie-break.

    ``expected_method_ids`` is mandatory so a detector that silently omits a
    method cannot redefine the calibration problem by changing the input.
    """

    frozen = tuple(members)
    if not frozen:
        raise ValueError("threshold calibration requires TRAIN members")
    methods = _exact_methods(expected_method_ids)
    _validate_membership(frozen)
    labels = {member.target_present for member in frozen}
    if labels != {False, True}:
        raise ValueError("threshold calibration requires both binary classes")

    maxima: dict[str, list[tuple[float, bool]]] = {method: [] for method in methods}
    semantics: dict[str, set[str]] = {method: set() for method in methods}
    for member in frozen:
        scores: dict[str, list[float]] = {}
        for score in member.bundle.method_scores:
            method = f"{score.method_id}@{score.method_version}"
            scores.setdefault(method, []).append(score.score)
            semantics.setdefault(method, set()).add(score.score_semantics)
        observed = set(scores)
        expected = set(methods)
        if observed != expected:
            missing = sorted(expected - observed)
            unexpected = sorted(observed - expected)
            raise ValueError(
                "FeatureSet method membership differs: "
                f"missing={missing}, unexpected={unexpected}"
            )
        for method in methods:
            maxima[method].append((max(scores[method]), member.target_present))

    inconsistent = sorted(method for method in methods if len(semantics[method]) != 1)
    if inconsistent:
        raise ValueError(
            f"calibration methods have inconsistent score semantics: {inconsistent}"
        )

    thresholds = tuple(
        (method, _balanced_accuracy_threshold(maxima[method])) for method in methods
    )
    membership_digest = canonical_digest(
        {
            "policy": CALIBRATION_POLICY,
            "expected_method_ids": methods,
            "members": tuple(
                sorted(
                    (_member_identity(member) for member in frozen),
                    key=lambda value: value["feature_set_id"],
                )
            ),
        }
    )
    calibration_dataset_id = f"dataset_traincal_{membership_digest.value[:32]}"
    rule_identity = canonical_digest(
        {
            "policy": CALIBRATION_POLICY,
            "calibration_membership_digest": str(membership_digest),
            "thresholds": thresholds,
        }
    )
    return ThresholdRule(
        rule_id=f"rule_traincal_{rule_identity.value[:32]}",
        calibration_dataset_id=calibration_dataset_id,
        thresholds=thresholds,
    )


def _exact_methods(methods: tuple[str, ...]) -> tuple[str, ...]:
    if not methods or len(methods) != len(set(methods)):
        raise ValueError("expected method identities must be non-empty and unique")
    for method in methods:
        if (
            not method
            or method.count("@") != 1
            or any(character.isspace() for character in method)
            or any(not part for part in method.split("@"))
        ):
            raise ValueError(
                "expected methods must use exact method@version identities"
            )
    return tuple(sorted(methods))


def _validate_membership(members: tuple[FrozenTrainCalibrationMember, ...]) -> None:
    feature_ids = [str(member.bundle.feature_set_id) for member in members]
    if len(feature_ids) != len(set(feature_ids)):
        raise ValueError("TRAIN feature-set membership must be unique")
    recording_ids = [str(member.bundle.recording_id) for member in members]
    if len(recording_ids) != len(set(recording_ids)):
        raise ValueError("each TRAIN recording must contribute exactly one FeatureSet")


def _member_identity(member: FrozenTrainCalibrationMember) -> dict[str, object]:
    bundle = member.bundle
    return {
        "feature_set_id": str(bundle.feature_set_id),
        "analysis_run_id": str(bundle.analysis_run_id),
        "feature_set_digest": str(Digest.sha256(encode_feature_set(bundle))),
        "recording_id": str(bundle.recording_id),
        "input_recording_identity_digest": str(bundle.input_recording_identity_digest),
        "split_group_id": member.split_group_id,
        "split": member.split.value,
        "target_present": member.target_present,
    }


def _balanced_accuracy_threshold(values: list[tuple[float, bool]]) -> float:
    positive_count = sum(label for _, label in values)
    negative_count = len(values) - positive_count
    if not positive_count or not negative_count:
        raise ValueError("threshold calibration requires both binary classes")
    candidates = {score for score, _ in values}
    above_maximum = math.nextafter(max(candidates), math.inf)
    if math.isfinite(above_maximum):
        candidates.add(above_maximum)

    def objective(threshold: float) -> tuple[int, float]:
        true_positive = sum(score >= threshold and label for score, label in values)
        true_negative = sum(score < threshold and not label for score, label in values)
        # The common positive_count * negative_count denominator can be omitted.
        balanced_accuracy_numerator = (
            true_positive * negative_count + true_negative * positive_count
        )
        return balanced_accuracy_numerator, threshold

    return max(candidates, key=objective)
