from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.recording.report_era_fire import (
    REPORT_ERA_SCORE_SEMANTICS_V1,
    decide_provisional_report_era_fire_v0_1,
)
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod
from leo_flow.contracts.starlink_report_era import ProvisionalReportEraFireState


def _decision():
    return decide_provisional_report_era_fire_v0_1(
        method=StarlinkDetectorMethod.DIFFERENTIAL_16,
        sample_rate_hz=5_000_000.0,
        probe_sample_count=800_000,
        reported_score=1.0,
        score_semantics=REPORT_ERA_SCORE_SEMANTICS_V1,
    )


def test_contract_cannot_be_relabelled_as_a_detection() -> None:
    with pytest.raises(ValueError, match="provisional label"):
        replace(_decision(), public_label="Starlink beacon detected")


def test_contract_cannot_invert_the_strict_threshold_result() -> None:
    with pytest.raises(ValueError, match="threshold comparison is inconsistent"):
        replace(
            _decision(),
            state=ProvisionalReportEraFireState.DID_NOT_FIRE,
            candidate_fire=False,
        )


def test_contract_requires_detection_denial_reason() -> None:
    with pytest.raises(ValueError, match="deny detection semantics"):
        replace(_decision(), reason_codes=("candidate-only",))
