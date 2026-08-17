"""Pure report-compatible calibration of Starlink suite whole-search scores."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from statistics import NormalDist

from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    RadioId,
    ReceiverChainId,
    SchemaRef,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import (
    StarlinkDetectorMethod,
    StarlinkDetectorMethodEvidenceV0_2,
    StarlinkDetectorSuiteBundleV0_2,
)
from leo_flow.contracts.starlink_suite_calibration import (
    ApprovedStarlinkSuiteCalibrationV0_1,
    StarlinkSuiteCalibrationCellPlanV0_1,
    StarlinkSuiteCalibrationEvidenceV0_1,
    StarlinkSuiteMethodDecisionV0_1,
    StarlinkSuitePositiveGateV0_1,
    StarlinkSuitePositivePerformanceV0_1,
    StarlinkSuiteSearchProfileV0_1,
)


def starlink_suite_search_profile_v0_1(
    suite: StarlinkDetectorSuiteBundleV0_2,
    method: StarlinkDetectorMethodEvidenceV0_2,
) -> StarlinkSuiteSearchProfileV0_1:
    """Derive a reusable search profile without recording/segment identity."""

    return StarlinkSuiteSearchProfileV0_1(
        SchemaRef(StarlinkSuiteSearchProfileV0_1.SCHEMA_ID, V0_1),
        method.method,
        method.search_mode,
        method.selection_method,
        method.effective_search_cell_count,
        suite.sample_rate_hz,
        suite.probe_sample_count,
        suite.edge,
        method.pilot_symbol_indices,
        method.symbol_set_role,
        method.symbol_split_digest,
        method.control_conditioning,
        method.algorithm_ref.digest,
        method.config_ref.digest,
        method.exact_template_ref.digest,
        method.conditioned_control_template_ref.digest,
    )


def plan_starlink_suite_calibration_cell_v0_1(
    *,
    cell_id: str,
    radio_id: RadioId,
    receiver_chain_id: ReceiverChainId,
    channel_number: int,
    edge: StarlinkEdge,
    sample_rate_hz: float,
    probe_sample_count: int,
    method: StarlinkDetectorMethod,
    hardware_profile_digest: Digest,
    tuning_identity_digest: Digest,
    algorithm_digest: Digest,
    config_digest: Digest,
    exact_template_digest: Digest,
    conditioned_control_template_digest: Digest,
    search_identity_digest: Digest,
    positive_gates: tuple[StarlinkSuitePositiveGateV0_1, ...],
    target_whole_search_far: float = 0.01,
    null_confidence_level: float = 0.95,
    expected_training_tail_count: int = 100,
    holdout_design_far_fraction: float = 0.5,
    expected_holdout_exceedance_count: int = 20,
) -> StarlinkSuiteCalibrationCellPlanV0_1:
    """Materialize resolvable train and confidence-qualified holdout counts."""

    if not 0 < target_whole_search_far < 1:
        raise ValueError("target_whole_search_far must lie in (0, 1)")
    if expected_training_tail_count <= 0:
        raise ValueError("expected_training_tail_count must be positive")
    from leo_flow.analysis.recording.starlink_calibration import (
        minimum_holdout_searches,
    )

    return StarlinkSuiteCalibrationCellPlanV0_1(
        SchemaRef(StarlinkSuiteCalibrationCellPlanV0_1.SCHEMA_ID, V0_1),
        cell_id,
        radio_id,
        receiver_chain_id,
        channel_number,
        edge,
        sample_rate_hz,
        probe_sample_count,
        method,
        hardware_profile_digest,
        tuning_identity_digest,
        algorithm_digest,
        config_digest,
        exact_template_digest,
        conditioned_control_template_digest,
        search_identity_digest,
        "whole-search-reported-score",
        "strict-greater-than",
        target_whole_search_far,
        null_confidence_level,
        math.ceil(expected_training_tail_count / target_whole_search_far),
        minimum_holdout_searches(
            target_whole_search_far,
            confidence_level=null_confidence_level,
            design_far_fraction=holdout_design_far_fraction,
            minimum_design_exceedance_count=expected_holdout_exceedance_count,
        ),
        positive_gates,
    )


def one_sided_wilson_lower_bound(
    success_count: int,
    trial_count: int,
    *,
    confidence_level: float,
) -> float:
    """Dependency-free one-sided lower confidence bound for injection Pd."""

    if trial_count <= 0 or not 0 <= success_count <= trial_count:
        raise ValueError("success count is outside the positive trial count")
    if not 0.5 < confidence_level < 1:
        raise ValueError("confidence_level must lie in (0.5, 1)")
    z = NormalDist().inv_cdf(confidence_level)
    proportion = success_count / trial_count
    z_squared = z * z
    denominator = 1 + z_squared / trial_count
    center = (proportion + z_squared / (2 * trial_count)) / denominator
    radius = (
        z
        / denominator
        * math.sqrt(
            proportion * (1 - proportion) / trial_count
            + z_squared / (4 * trial_count * trial_count)
        )
    )
    return max(0.0, center - radius)


def evaluate_starlink_suite_calibration_v0_1(
    plan: StarlinkSuiteCalibrationCellPlanV0_1,
    *,
    corpus_digest: Digest,
    training_null_whole_search_scores: Sequence[float],
    holdout_null_whole_search_scores: Sequence[float],
    positive_whole_search_scores_by_snr_db: Mapping[float, Sequence[float]],
) -> StarlinkSuiteCalibrationEvidenceV0_1:
    """Fit one method threshold on train nulls, then audit holdout and Pd."""

    training = _scores(
        training_null_whole_search_scores,
        plan.training_null_search_count,
        "training null",
    )
    holdout = _scores(
        holdout_null_whole_search_scores,
        plan.holdout_null_search_count,
        "holdout null",
    )
    expected_snrs = tuple(item.injection_snr_db for item in plan.positive_gates)
    if tuple(sorted(positive_whole_search_scores_by_snr_db)) != expected_snrs:
        raise ValueError("positive score SNR grid differs from the cell plan")
    allowed = math.floor(len(training) * plan.target_whole_search_far)
    if allowed <= 0:
        raise ValueError("training corpus has no resolvable target-FAR tail")
    threshold = sorted(training, reverse=True)[allowed - 1]
    train_exceedances = sum(value > threshold for value in training)
    holdout_exceedances = sum(value > threshold for value in holdout)

    from leo_flow.analysis.recording.starlink_calibration import (
        one_sided_wilson_upper_bound,
    )

    far_upper = one_sided_wilson_upper_bound(
        holdout_exceedances,
        len(holdout),
        confidence_level=plan.null_confidence_level,
    )
    positive = []
    for gate in plan.positive_gates:
        scores = _scores(
            positive_whole_search_scores_by_snr_db[gate.injection_snr_db],
            gate.trial_count,
            "positive injection",
        )
        detections = sum(value > threshold for value in scores)
        lower = one_sided_wilson_lower_bound(
            detections,
            len(scores),
            confidence_level=gate.confidence_level,
        )
        positive.append(
            StarlinkSuitePositivePerformanceV0_1(
                gate.injection_snr_db,
                len(scores),
                detections,
                lower,
                gate.confidence_level,
                gate.minimum_detection_probability,
                lower >= gate.minimum_detection_probability,
            )
        )
    accepted = far_upper <= plan.target_whole_search_far and all(
        item.accepted for item in positive
    )
    identity = Digest.sha256(
        (str(plan.digest) + str(corpus_digest) + repr(threshold)).encode()
    ).value
    return StarlinkSuiteCalibrationEvidenceV0_1(
        SchemaRef(StarlinkSuiteCalibrationEvidenceV0_1.SCHEMA_ID, V0_1),
        f"slsuitecalevidence_{identity[:32]}",
        plan.digest,
        corpus_digest,
        threshold,
        plan.threshold_comparison,
        len(training),
        train_exceedances,
        len(holdout),
        holdout_exceedances,
        far_upper,
        plan.target_whole_search_far,
        plan.null_confidence_level,
        tuple(positive),
        accepted,
    )


def approve_starlink_suite_calibration_v0_1(
    plan: StarlinkSuiteCalibrationCellPlanV0_1,
    evidence: StarlinkSuiteCalibrationEvidenceV0_1,
    *,
    calibration_id: str,
) -> ApprovedStarlinkSuiteCalibrationV0_1:
    if evidence.cell_plan_digest != plan.digest:
        raise ValueError("suite calibration evidence belongs to another cell")
    if not evidence.accepted:
        raise ValueError("suite calibration null or positive gate failed")
    return ApprovedStarlinkSuiteCalibrationV0_1(
        SchemaRef(ApprovedStarlinkSuiteCalibrationV0_1.SCHEMA_ID, V0_1),
        calibration_id,
        plan,
        ArtifactRef(
            evidence.evidence_id,
            evidence.digest,
            SchemaRef(StarlinkSuiteCalibrationEvidenceV0_1.SCHEMA_ID, V0_1),
        ),
        evidence.corpus_digest,
        evidence.threshold,
    )


def decide_starlink_suite_method_v0_1(
    suite: StarlinkDetectorSuiteBundleV0_2,
    method: StarlinkDetectorMethodEvidenceV0_2,
    calibration: ApprovedStarlinkSuiteCalibrationV0_1,
    *,
    radio_id: RadioId,
    channel_number: int,
    hardware_profile_digest: Digest,
    tuning_identity_digest: Digest,
) -> StarlinkSuiteMethodDecisionV0_1:
    """Apply one approved exact-cell threshold; never infer an event count."""

    plan = calibration.cell_plan
    checks = (
        (radio_id == plan.radio_id, "radio"),
        (suite.receiver_chain_id == plan.receiver_chain_id, "receiver"),
        (channel_number == plan.channel_number, "channel"),
        (suite.edge is plan.edge, "edge"),
        (suite.sample_rate_hz == plan.sample_rate_hz, "sample rate"),
        (suite.probe_sample_count == plan.probe_sample_count, "probe"),
        (method.method is plan.method, "method"),
        (hardware_profile_digest == plan.hardware_profile_digest, "hardware"),
        (tuning_identity_digest == plan.tuning_identity_digest, "tuning"),
        (method.algorithm_ref.digest == plan.algorithm_digest, "algorithm"),
        (method.config_ref.digest == plan.config_digest, "config"),
        (method.exact_template_ref.digest == plan.exact_template_digest, "template"),
        (
            method.conditioned_control_template_ref.digest
            == plan.conditioned_control_template_digest,
            "control template",
        ),
        (
            starlink_suite_search_profile_v0_1(suite, method).digest
            == plan.search_identity_digest,
            "search profile",
        ),
    )
    for passed, label in checks:
        if not passed:
            raise ValueError(f"suite calibration {label} identity differs")
    score = method.reported_score
    return StarlinkSuiteMethodDecisionV0_1(
        SchemaRef(StarlinkSuiteMethodDecisionV0_1.SCHEMA_ID, V0_1),
        suite.ref,
        calibration.ref,
        method.method,
        score,
        calibration.threshold,
        plan.threshold_comparison,
        score > calibration.threshold,
        (
            "calibrated-whole-search-method-decision",
            "method-decision-not-beacon-count",
            "event-clustering-required",
        ),
    )


def _scores(values: Sequence[float], expected: int, label: str) -> tuple[float, ...]:
    scores = tuple(float(value) for value in values)
    if len(scores) != expected:
        raise ValueError(f"{label} score count differs from the cell plan")
    if any(not math.isfinite(value) for value in scores):
        raise ValueError(f"{label} scores must be finite")
    return scores
