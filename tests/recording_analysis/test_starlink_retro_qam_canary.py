from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.analysis.recording.api import AnalysisExecutionContext
from leo_flow.analysis.recording.starlink_retro_qam_canary import (
    RetroQamCombinedExpectationV0_1,
    analyze_starlink_retro_qam_canary_v0_1,
)
from leo_flow.contracts.core import Digest, UtcNs
from leo_flow.deployments.starlink_retro_qam_canary import _load_request

MANIFEST = Path(__file__).parent / "fixtures/retro_qam_2026_08_17_v1.json"


def _execution() -> AnalysisExecutionContext:
    return AnalysisExecutionContext(
        "retro-qam-test",
        "0.1.0",
        "0123456789abcdef",
        Digest.sha256(b"test-environment"),
        UtcNs(1),
        UtcNs(2),
        "test-host",
    )


@pytest.mark.integration
def test_native_search_qam_and_dual_rx_metrics_match_historical_oracle() -> None:
    if not os.path.isdir("/mnt/qnap01/mouse9911/leo-store/2026_08_17_RETRO_QAM"):
        pytest.skip("read-only RETRO QAM corpus is not mounted")
    request = _load_request(MANIFEST)
    receipt = analyze_starlink_retro_qam_canary_v0_1(request, _execution())

    assert receipt.metrics_match_oracle is True
    assert receipt.candidate_only is True
    assert receipt.calibrated_detection is None
    assert [item.winning_epoch_sample for item in receipt.receivers] == [2063, 2063]
    assert receipt.receivers[0].winning_cfo_hz == pytest.approx(
        364_150.8476787003, abs=35
    )
    assert receipt.receivers[1].winning_cfo_hz == pytest.approx(
        -194_343.8743595247, abs=35
    )
    assert receipt.receivers[0].hard_symbol_accuracy == pytest.approx(
        0.7483333333333333, abs=1 / 2_400
    )
    assert receipt.receivers[1].hard_symbol_accuracy == pytest.approx(
        0.7991666666666667, abs=1 / 2_400
    )
    assert receipt.combined.hard_symbol_accuracy == pytest.approx(
        0.88375, abs=2 / 2_400
    )
    assert receipt.combined.rms_evm == pytest.approx(0.6380024919780618, abs=5e-4)
    assert receipt.combined.soft_mean_confidence == pytest.approx(
        0.8936395049095154, abs=1e-4
    )


@pytest.mark.integration
def test_oracle_mismatch_fails_result_without_becoming_detection() -> None:
    if not os.path.isdir("/mnt/qnap01/mouse9911/leo-store/2026_08_17_RETRO_QAM"):
        pytest.skip("read-only RETRO QAM corpus is not mounted")
    request = _load_request(MANIFEST)
    request = replace(
        request,
        combined_expectation=RetroQamCombinedExpectationV0_1(0.99, 0.01, 0.99),
    )
    receipt = analyze_starlink_retro_qam_canary_v0_1(request, _execution())
    assert receipt.metrics_match_oracle is False
    assert receipt.candidate_only is True
    assert receipt.calibrated_detection is None
