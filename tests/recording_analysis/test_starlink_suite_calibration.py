from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.recording.starlink_suite_calibration import (
    approve_starlink_suite_calibration_v0_1,
    decide_starlink_suite_method_v0_1,
    evaluate_starlink_suite_calibration_v0_1,
    one_sided_wilson_lower_bound,
    plan_starlink_suite_calibration_cell_v0_1,
)
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    Provenance,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import (
    REPORT_METHOD_ORDER,
    V0_2,
    StarlinkDetectorMethod,
    StarlinkDetectorMethodEvidenceV0_2,
    StarlinkDetectorSuiteBundleV0_2,
    StarlinkFrameScoreSummaryV0_2,
    StarlinkSamplingStratum,
    StarlinkSearchMode,
)
from leo_flow.contracts.starlink_suite_calibration import (
    StarlinkSuiteCalibrationCellPlanV0_1,
    StarlinkSuitePositiveGateV0_1,
)


def _digest(label: str) -> Digest:
    return Digest.sha256(label.encode())


def _ref(identity: str, digest: Digest) -> ArtifactRef:
    return ArtifactRef(identity, digest, SchemaRef("org.leo-flow.test-artifact", V0_1))


def _method(method: StarlinkDetectorMethod, score: float = 0.9):
    role = {
        StarlinkDetectorMethod.ANCHOR_8: "anchor",
        StarlinkDetectorMethod.DIFFERENTIAL_16: "contiguous",
        StarlinkDetectorMethod.DIFFERENTIAL_32: "contiguous",
        StarlinkDetectorMethod.GLRT_32: "contiguous",
        StarlinkDetectorMethod.GLRT_64: "contiguous",
        StarlinkDetectorMethod.FULL_FRAME_ACQUIRE: "acquire",
        StarlinkDetectorMethod.FULL_FRAME_VERIFY: "verify",
        StarlinkDetectorMethod.FULL_FRAME_FULL: "full",
    }[method]
    split = _digest("split") if role in ("acquire", "verify", "full") else None
    searched = method not in (
        StarlinkDetectorMethod.FULL_FRAME_VERIFY,
        StarlinkDetectorMethod.FULL_FRAME_FULL,
    )
    control = 0.1
    return StarlinkDetectorMethodEvidenceV0_2(
        SchemaRef(StarlinkDetectorMethodEvidenceV0_2.SCHEMA_ID, V0_2),
        method,
        _ref("algorithm", _digest("algorithm")),
        _ref("config", _digest("config")),
        _ref("exact", _digest("exact")),
        _ref("roll17", _digest("roll17")),
        _digest("whole-search"),
        (
            StarlinkSearchMode.SEARCHED_EXACT
            if searched
            else StarlinkSearchMode.CONDITIONED_ON_ACQUIRE_WINNER
        ),
        method if searched else StarlinkDetectorMethod.FULL_FRAME_ACQUIRE,
        128,
        12,
        1000.0,
        0.0,
        score,
        score,
        control,
        score - control,
        StarlinkFrameScoreSummaryV0_2(score, score, 2),
        StarlinkFrameScoreSummaryV0_2(control, control, 2),
        (2, 3),
        role,
        split,
        "exact-winning-epoch-coarse-and-residual-cfo-fixed",
        True,
        ("whole-search-calibration-required",),
    )


def _suite(score: float = 0.9) -> StarlinkDetectorSuiteBundleV0_2:
    methods = tuple(_method(method, score) for method in REPORT_METHOD_ORDER)
    return StarlinkDetectorSuiteBundleV0_2(
        SchemaRef(StarlinkDetectorSuiteBundleV0_2.SCHEMA_ID, V0_2),
        "slsuite_stream_fixture",
        RecordingId("rec_suitecalibration"),
        _digest("recording"),
        SegmentId("seg_suitecalibration"),
        ReceiverChainId("rx_lnb_a"),
        StarlinkEdge.LOWER,
        2_500_000.0,
        20_000,
        StarlinkSamplingStratum.FULL_PILOT_BAND,
        _digest("suite-identity"),
        _digest("split"),
        methods,
        None,
        Provenance(
            "suite-calibration-test",
            "0.1",
            "commit",
            _digest("environment"),
            _digest("normalized-config"),
            (_digest("recording"),),
            (),
            UtcNs(1),
            UtcNs(2),
            "test-host",
        ),
        True,
        (),
    )


def _plan(suite: StarlinkDetectorSuiteBundleV0_2):
    item = suite.methods[0]
    return StarlinkSuiteCalibrationCellPlanV0_1(
        SchemaRef(StarlinkSuiteCalibrationCellPlanV0_1.SCHEMA_ID, V0_1),
        "slsuitecalcell_fixture",
        RadioId("radio_pluto_5d4d"),
        suite.receiver_chain_id,
        1,
        suite.edge,
        suite.sample_rate_hz,
        suite.probe_sample_count,
        item.method,
        _digest("hardware"),
        _digest("tuning"),
        item.algorithm_ref.digest,
        item.config_ref.digest,
        item.exact_template_ref.digest,
        item.conditioned_control_template_ref.digest,
        item.search_identity_digest,
        "whole-search-reported-score",
        "strict-greater-than",
        0.2,
        0.8,
        10,
        10,
        (StarlinkSuitePositiveGateV0_1(-6.0, 10, 0.7, 0.8),),
    )


def _evidence(plan):
    return evaluate_starlink_suite_calibration_v0_1(
        plan,
        corpus_digest=_digest("locked-disjoint-corpus"),
        training_null_whole_search_scores=tuple(index / 10 for index in range(10)),
        holdout_null_whole_search_scores=(0.1,) * 10,
        positive_whole_search_scores_by_snr_db={-6.0: (1.0,) * 10},
    )


def test_report_score_calibration_passes_disjoint_far_and_positive_gates() -> None:
    suite = _suite()
    plan = _plan(suite)
    evidence = _evidence(plan)
    assert evidence.threshold == 0.8
    assert evidence.training_threshold_exceedance_count == 1
    assert evidence.holdout_threshold_exceedance_count == 0
    assert evidence.positive_performance[0].accepted is True
    assert evidence.accepted is True

    approved = approve_starlink_suite_calibration_v0_1(
        plan, evidence, calibration_id="slsuitecalibration_fixture"
    )
    decision = decide_starlink_suite_method_v0_1(
        suite,
        suite.methods[0],
        approved,
        radio_id=plan.radio_id,
        channel_number=plan.channel_number,
        hardware_profile_digest=plan.hardware_profile_digest,
        tuning_identity_digest=plan.tuning_identity_digest,
    )
    assert decision.detected is True
    assert decision.score == 0.9
    assert "method-decision-not-beacon-count" in decision.reason_codes


def test_strict_report_threshold_does_not_fire_on_a_tie() -> None:
    suite = _suite(score=0.8)
    plan = _plan(suite)
    evidence = _evidence(plan)
    approved = approve_starlink_suite_calibration_v0_1(
        plan, evidence, calibration_id="slsuitecalibration_tie"
    )
    decision = decide_starlink_suite_method_v0_1(
        suite,
        suite.methods[0],
        approved,
        radio_id=plan.radio_id,
        channel_number=1,
        hardware_profile_digest=plan.hardware_profile_digest,
        tuning_identity_digest=plan.tuning_identity_digest,
    )
    assert decision.detected is False


def test_calibration_rejects_identity_drift_and_failed_positive_gate() -> None:
    suite = _suite()
    plan = _plan(suite)
    evidence = evaluate_starlink_suite_calibration_v0_1(
        plan,
        corpus_digest=_digest("weak-positive-corpus"),
        training_null_whole_search_scores=tuple(index / 10 for index in range(10)),
        holdout_null_whole_search_scores=(0.1,) * 10,
        positive_whole_search_scores_by_snr_db={-6.0: (0.0,) * 10},
    )
    assert evidence.accepted is False
    with pytest.raises(ValueError, match="null or positive gate failed"):
        approve_starlink_suite_calibration_v0_1(
            plan, evidence, calibration_id="slsuitecalibration_rejected"
        )

    approved = approve_starlink_suite_calibration_v0_1(
        plan, _evidence(plan), calibration_id="slsuitecalibration_fixture"
    )
    with pytest.raises(ValueError, match="sample rate identity differs"):
        decide_starlink_suite_method_v0_1(
            replace(suite, sample_rate_hz=5_000_000.0),
            suite.methods[0],
            approved,
            radio_id=plan.radio_id,
            channel_number=1,
            hardware_profile_digest=plan.hardware_profile_digest,
            tuning_identity_digest=plan.tuning_identity_digest,
        )


def test_positive_confidence_bound_and_corpus_shape_fail_closed() -> None:
    assert one_sided_wilson_lower_bound(10, 10, confidence_level=0.8) > 0.7
    plan = _plan(_suite())
    with pytest.raises(ValueError, match="SNR grid"):
        evaluate_starlink_suite_calibration_v0_1(
            plan,
            corpus_digest=_digest("corpus"),
            training_null_whole_search_scores=(0.0,) * 10,
            holdout_null_whole_search_scores=(0.0,) * 10,
            positive_whole_search_scores_by_snr_db={-3.0: (1.0,) * 10},
        )


def test_default_planner_resolves_one_percent_whole_search_tail() -> None:
    suite = _suite()
    method = suite.methods[0]
    plan = plan_starlink_suite_calibration_cell_v0_1(
        cell_id="slsuitecalcell_planned",
        radio_id=RadioId("radio_pluto_5d4d"),
        receiver_chain_id=suite.receiver_chain_id,
        channel_number=1,
        edge=suite.edge,
        sample_rate_hz=suite.sample_rate_hz,
        probe_sample_count=suite.probe_sample_count,
        method=method.method,
        hardware_profile_digest=_digest("hardware"),
        tuning_identity_digest=_digest("tuning"),
        algorithm_digest=method.algorithm_ref.digest,
        config_digest=method.config_ref.digest,
        exact_template_digest=method.exact_template_ref.digest,
        conditioned_control_template_digest=(
            method.conditioned_control_template_ref.digest
        ),
        search_identity_digest=method.search_identity_digest,
        positive_gates=(StarlinkSuitePositiveGateV0_1(-6.0, 100, 0.8, 0.95),),
    )
    assert plan.training_null_search_count == 10_000
    assert plan.holdout_null_search_count == 4_000
