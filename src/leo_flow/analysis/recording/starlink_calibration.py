"""Post-capture Starlink threshold planning and held-out FAR evaluation."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from statistics import NormalDist

from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    RadioId,
    ReceiverChainId,
    SchemaRef,
    canonical_digest,
)
from leo_flow.contracts.starlink import (
    StarlinkEdge,
    StarlinkPilotCalibrationV0_1,
)
from leo_flow.contracts.starlink_calibration import (
    StarlinkCalibrationCellPlanV0_1,
    StarlinkCalibrationEvidenceV0_1,
    StarlinkPositivePerformanceV0_1,
)


def one_sided_wilson_upper_bound(
    exceedance_count: int,
    search_count: int,
    *,
    confidence_level: float = 0.95,
) -> float:
    """Dependency-free one-sided binomial upper confidence bound."""

    if search_count <= 0 or not 0 <= exceedance_count <= search_count:
        raise ValueError("exceedance count is outside the holdout search count")
    if not 0.5 < confidence_level < 1:
        raise ValueError("confidence_level must lie in (0.5, 1)")
    z = NormalDist().inv_cdf(confidence_level)
    proportion = exceedance_count / search_count
    z_squared = z * z
    denominator = 1 + z_squared / search_count
    center = (proportion + z_squared / (2 * search_count)) / denominator
    radius = (
        z
        / denominator
        * math.sqrt(
            proportion * (1 - proportion) / search_count
            + z_squared / (4 * search_count * search_count)
        )
    )
    return min(1.0, center + radius)


def minimum_holdout_searches(
    target_far: float,
    *,
    confidence_level: float = 0.95,
    design_far_fraction: float = 0.5,
    minimum_design_exceedance_count: int = 20,
) -> int:
    """Plan N so the Wilson upper bound passes at the lower design FAR."""

    if not 0 < target_far < 1:
        raise ValueError("target_far must lie in (0, 1)")
    if not 0 < design_far_fraction < 1:
        raise ValueError("design_far_fraction must lie in (0, 1)")
    if minimum_design_exceedance_count <= 0:
        raise ValueError("minimum_design_exceedance_count must be positive")
    design_far = target_far * design_far_fraction
    z = NormalDist().inv_cdf(confidence_level)
    gap = target_far - design_far
    count = max(
        math.ceil(minimum_design_exceedance_count / design_far),
        math.ceil(z * z * design_far * (1 - design_far) / (gap * gap)),
    )
    while (
        one_sided_wilson_upper_bound(
            math.floor(count * design_far),
            count,
            confidence_level=confidence_level,
        )
        > target_far
    ):
        count += 1
    return count


def plan_starlink_calibration_cell_v0_1(
    *,
    cell_id: str,
    radio_id: RadioId,
    receiver_chain_id: ReceiverChainId,
    hardware_profile_digest: Digest,
    tuning_identity_digest: Digest,
    channel_number: int,
    edge: StarlinkEdge,
    algorithm_digest: Digest,
    config_digest: Digest,
    exact_template_digest: Digest,
    conditioned_control_template_digest: Digest,
    search_identity_digest: Digest,
    positive_injection_plan_ref: ArtifactRef,
    target_whole_search_far: float = 0.01,
    confidence_level: float = 0.95,
    expected_training_tail_count: int = 100,
    expected_holdout_exceedance_count: int = 20,
    holdout_design_far_fraction: float = 0.5,
) -> StarlinkCalibrationCellPlanV0_1:
    """Produce checked train/holdout counts for one exact statistical cell."""

    if expected_training_tail_count <= 0:
        raise ValueError("expected_training_tail_count must be positive")
    training_count = math.ceil(expected_training_tail_count / target_whole_search_far)
    holdout_count = minimum_holdout_searches(
        target_whole_search_far,
        confidence_level=confidence_level,
        design_far_fraction=holdout_design_far_fraction,
        minimum_design_exceedance_count=expected_holdout_exceedance_count,
    )
    return StarlinkCalibrationCellPlanV0_1(
        SchemaRef(StarlinkCalibrationCellPlanV0_1.SCHEMA_ID, V0_1),
        cell_id,
        radio_id,
        receiver_chain_id,
        hardware_profile_digest,
        tuning_identity_digest,
        channel_number,
        edge,
        algorithm_digest,
        config_digest,
        exact_template_digest,
        conditioned_control_template_digest,
        search_identity_digest,
        target_whole_search_far,
        confidence_level,
        training_count,
        holdout_count,
        expected_training_tail_count,
        expected_holdout_exceedance_count,
        target_whole_search_far * holdout_design_far_fraction,
        positive_injection_plan_ref,
        "ceil-expected-tail-count-divided-by-target-whole-search-far",
        "minimum-one-sided-wilson-upper-at-design-far",
    )


def evaluate_calibration_cell_v0_1(
    plan: StarlinkCalibrationCellPlanV0_1,
    *,
    corpus_digest: Digest,
    training_null_scores: Sequence[float],
    holdout_null_scores: Sequence[float],
    positive_scores_by_snr_db: Mapping[float, Sequence[float]],
) -> StarlinkCalibrationEvidenceV0_1:
    """Fit on locked training nulls and test once on disjoint holdout nulls."""

    if len(training_null_scores) != plan.training_null_search_count:
        raise ValueError("training null count differs from the calibration plan")
    if len(holdout_null_scores) != plan.holdout_null_search_count:
        raise ValueError("holdout null count differs from the calibration plan")
    training = _finite_scores(training_null_scores, "training null")
    holdout = _finite_scores(holdout_null_scores, "holdout null")
    allowed = math.floor(len(training) * plan.target_whole_search_far)
    descending = sorted(training, reverse=True)
    threshold = math.nextafter(descending[max(0, allowed - 1)], math.inf)
    train_exceedances = sum(score >= threshold for score in training)
    holdout_exceedances = sum(score >= threshold for score in holdout)
    upper = one_sided_wilson_upper_bound(
        holdout_exceedances,
        len(holdout),
        confidence_level=plan.confidence_level,
    )
    positive = tuple(
        StarlinkPositivePerformanceV0_1(
            float(snr),
            len(scores),
            sum(score >= threshold for score in _finite_scores(scores, "positive")),
        )
        for snr, scores in sorted(positive_scores_by_snr_db.items())
    )
    identity = canonical_digest(
        {
            "cell_plan_digest": str(plan.digest),
            "corpus_digest": str(corpus_digest),
            "threshold": threshold,
        }
    ).value
    return StarlinkCalibrationEvidenceV0_1(
        SchemaRef(StarlinkCalibrationEvidenceV0_1.SCHEMA_ID, V0_1),
        f"slcalevidence_{identity[:32]}",
        plan.digest,
        corpus_digest,
        threshold,
        len(training),
        train_exceedances,
        len(holdout),
        holdout_exceedances,
        upper,
        plan.confidence_level,
        plan.target_whole_search_far,
        "one-sided-wilson-score",
        upper <= plan.target_whole_search_far,
        positive,
    )


def approved_calibration_v0_1(
    plan: StarlinkCalibrationCellPlanV0_1,
    evidence: StarlinkCalibrationEvidenceV0_1,
    *,
    calibration_id: str,
    null_dataset_digest: Digest,
    null_split_digest: Digest,
) -> StarlinkPilotCalibrationV0_1:
    """Promote held-out evidence only when its confidence gate passes."""

    if evidence.cell_plan_digest != plan.digest:
        raise ValueError("calibration evidence belongs to another cell plan")
    if not evidence.accepted:
        raise ValueError("held-out whole-search FAR confidence gate failed")
    return StarlinkPilotCalibrationV0_1(
        SchemaRef(StarlinkPilotCalibrationV0_1.SCHEMA_ID, V0_1),
        calibration_id,
        plan.algorithm_digest,
        plan.config_digest,
        plan.exact_template_digest,
        plan.conditioned_control_template_digest,
        plan.search_identity_digest,
        plan.hardware_profile_digest,
        null_dataset_digest,
        null_split_digest,
        "searched-exact-minus-conditioned-control-margin",
        "whole-search",
        evidence.threshold,
        plan.target_whole_search_far,
        evidence.holdout_null_search_count,
        evidence.holdout_threshold_exceedance_count,
    )


def _finite_scores(values: Iterable[float], label: str) -> tuple[float, ...]:
    scores = tuple(float(value) for value in values)
    if not scores:
        raise ValueError(f"{label} scores cannot be empty")
    if any(not math.isfinite(value) for value in scores):
        raise ValueError(f"{label} scores must be finite")
    return scores
