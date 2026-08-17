from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.contracts.core import (
    V0_1,
    AnalysisRunId,
    ArtifactRef,
    Digest,
    RecordingId,
    SchemaRef,
)
from leo_flow.contracts.starlink import (
    RecordingStarlinkDecisionViewV0_1,
    StarlinkEvaluationState,
    StarlinkPilotEvaluationV0_1,
    StarlinkRecordingDecisionState,
)


def _analysis_ref() -> ArtifactRef:
    return ArtifactRef(
        AnalysisRunId("arun_starlink_test"),
        Digest.sha256(b"analysis"),
        SchemaRef("org.leo-flow.starlink-pilot-analysis-bundle", V0_1),
    )


def test_dashboard_summary_cannot_count_uncalibrated_candidates() -> None:
    view = RecordingStarlinkDecisionViewV0_1(
        SchemaRef(RecordingStarlinkDecisionViewV0_1.SCHEMA_ID, V0_1),
        RecordingId("rec_starlink_test"),
        StarlinkRecordingDecisionState.CANDIDATES,
        2,
        2,
        None,
        _analysis_ref(),
        ("whole-search-calibration-required",),
    )

    assert view.search_candidate_count == 2
    assert view.calibrated_detection_count is None
    with pytest.raises(ValueError, match="cannot count detections"):
        replace(view, calibrated_detection_count=1)


def test_not_evaluated_summary_is_distinct_from_zero_detections() -> None:
    view = RecordingStarlinkDecisionViewV0_1(
        SchemaRef(RecordingStarlinkDecisionViewV0_1.SCHEMA_ID, V0_1),
        RecordingId("rec_starlink_test"),
        StarlinkRecordingDecisionState.NOT_EVALUATED,
        0,
        0,
        None,
        None,
        ("starlink-analysis-not-run",),
    )

    assert view.state is StarlinkRecordingDecisionState.NOT_EVALUATED
    assert view.calibrated_detection_count is None
    with pytest.raises(ValueError, match="not-evaluated"):
        replace(view, analyzed_stream_count=1)


def test_evaluation_contract_forbids_uncalibrated_boolean() -> None:
    evaluation = StarlinkPilotEvaluationV0_1(
        SchemaRef(StarlinkPilotEvaluationV0_1.SCHEMA_ID, V0_1),
        "slcandidate_test",
        Digest.sha256(b"candidate"),
        StarlinkEvaluationState.UNCALIBRATED,
        "searched-exact-minus-conditioned-control-margin",
        0.2,
        None,
        None,
        None,
        ("no-matching-calibration",),
    )

    with pytest.raises(ValueError, match="cannot emit a detection"):
        replace(evaluation, detected=False)
    with pytest.raises(ValueError, match="must cite its threshold"):
        replace(evaluation, state=StarlinkEvaluationState.CALIBRATED)
