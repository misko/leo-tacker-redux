from __future__ import annotations

import hashlib
from importlib.resources import files

import pytest

from leo_flow.analysis.recording.report_era_fire import (
    REPORT_ERA_SCORE_SEMANTICS_V1,
    REPORT_ERA_THRESHOLD_ARTIFACT_SHA256,
    decide_provisional_report_era_fire_v0_1,
)
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod
from leo_flow.contracts.starlink_report_era import ProvisionalReportEraFireState


def _decide(
    *,
    rate: float = 2_500_000.0,
    samples: int = 200_000,
    score: float = 1.0,
    semantics: str = REPORT_ERA_SCORE_SEMANTICS_V1,
):
    return decide_provisional_report_era_fire_v0_1(
        method=StarlinkDetectorMethod.ANCHOR_8,
        sample_rate_hz=rate,
        probe_sample_count=samples,
        reported_score=score,
        score_semantics=semantics,
    )


def test_vendored_artifact_is_the_exact_reviewed_reconstruction() -> None:
    raw = (
        files("leo_flow.analysis.recording")
        .joinpath("artifacts/report_era_thresholds_v1.json")
        .read_bytes()
    )
    assert hashlib.sha256(raw).hexdigest() == REPORT_ERA_THRESHOLD_ARTIFACT_SHA256
    assert REPORT_ERA_THRESHOLD_ARTIFACT_SHA256 == (
        "c8f64ab27c1fc2f4aa6a3b55f4bfdb68c72422c9833d6de9728c7f4e54268500"
    )


def test_exact_supported_cell_uses_strict_report_threshold() -> None:
    # Frozen 2.5 MS/s, 80 ms anchor-8 threshold.
    threshold = 0.22869712063703568
    equal = _decide(score=threshold)
    above = _decide(score=threshold + 1e-12)

    assert equal.state is ProvisionalReportEraFireState.DID_NOT_FIRE
    assert equal.candidate_fire is False
    assert equal.public_label == "provisional report-era candidate non-fire"
    assert above.state is ProvisionalReportEraFireState.FIRED
    assert above.candidate_fire is True
    assert "not-a-calibrated-beacon-detection" in above.reason_codes


@pytest.mark.parametrize(
    ("rate", "samples"),
    (
        (2_500_000.0, 20_000),  # deployed v0.2: 8 ms
        (2_500_000.0, 100_000),  # 40 ms has no report threshold
        (2_500_000.0, 1_600_000),  # 640 ms absent from the current corpus
        (1_250_000.0, 100_000),  # 80 ms, but the pilot band is clipped
        (10_000_000.0, 800_000),  # 80 ms absent from the current corpus
    ),
)
def test_unsupported_dimensions_fail_closed(rate: float, samples: int) -> None:
    decision = _decide(rate=rate, samples=samples)
    assert decision.state is ProvisionalReportEraFireState.NOT_APPLICABLE
    assert decision.threshold is None
    assert decision.candidate_fire is None
    assert "unsupported-report-era-dimensions" in decision.reason_codes


def test_incompatible_search_semantics_fail_closed() -> None:
    decision = _decide(semantics="redux-v0.2-eight-ms-suite")
    assert decision.state is ProvisionalReportEraFireState.NOT_APPLICABLE
    assert decision.threshold is None
    assert decision.candidate_fire is None
    assert "incompatible-report-era-search-semantics" in decision.reason_codes


@pytest.mark.parametrize("score", (-0.1, 1.1, float("nan")))
def test_invalid_scores_are_rejected(score: float) -> None:
    with pytest.raises(ValueError, match="reported_score"):
        _decide(score=score)


@pytest.mark.parametrize(
    ("rate", "samples"),
    (
        (2_500_000.0, 200_000),
        (2_500_000.0, 400_000),
        (5_000_000.0, 400_000),
        (5_000_000.0, 800_000),
    ),
)
def test_all_current_exact_overlap_dimensions_are_enabled(
    rate: float, samples: int
) -> None:
    decision = _decide(rate=rate, samples=samples)
    assert decision.state is ProvisionalReportEraFireState.FIRED
    assert decision.threshold is not None
