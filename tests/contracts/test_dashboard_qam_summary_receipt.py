from __future__ import annotations

import pytest

from leo_flow.contracts.core import Digest, DigestAlgorithm, RecordingId
from leo_flow.contracts.dashboard_qam_summary_receipt import (
    DASHBOARD_QAM_SUMMARY_CONFIG_REF_V0_2,
    DashboardQamSummaryReceiptV0_2,
    QamSummarySourceKind,
    QamSummaryTerminalOutcome,
    dashboard_qam_candidate_set_digest_v0_2,
)

_SHA = Digest(DigestAlgorithm.SHA256, "a" * 64)


def test_terminal_receipt_distinguishes_complete_from_no_candidate() -> None:
    complete = _receipt(QamSummaryTerminalOutcome.COMPLETE, candidate_count=2)
    empty = _receipt(QamSummaryTerminalOutcome.NO_CANDIDATE, candidate_count=0)

    assert complete.terminal_outcome is QamSummaryTerminalOutcome.COMPLETE
    assert empty.terminal_outcome is QamSummaryTerminalOutcome.NO_CANDIDATE
    assert empty.candidate_set_digest == dashboard_qam_candidate_set_digest_v0_2([])
    assert DASHBOARD_QAM_SUMMARY_CONFIG_REF_V0_2.digest.algorithm.value == "sha256"


@pytest.mark.parametrize(
    ("outcome", "candidate_count"),
    [
        (QamSummaryTerminalOutcome.COMPLETE, 0),
        (QamSummaryTerminalOutcome.NO_CANDIDATE, 1),
    ],
)
def test_terminal_receipt_rejects_ambiguous_candidate_count(
    outcome: QamSummaryTerminalOutcome, candidate_count: int
) -> None:
    with pytest.raises(ValueError, match="terminal outcome"):
        _receipt(outcome, candidate_count=candidate_count)


def _receipt(
    outcome: QamSummaryTerminalOutcome, *, candidate_count: int
) -> DashboardQamSummaryReceiptV0_2:
    return DashboardQamSummaryReceiptV0_2(
        source_kind=QamSummarySourceKind.ACQUIRED_V0_3,
        analysis_id="slqam3rec_" + "1" * 32,
        recording_id=RecordingId("rec_qam_summary_receipt"),
        source_request_digest=_SHA,
        source_product_digest=_SHA,
        summary_config_digest=DASHBOARD_QAM_SUMMARY_CONFIG_REF_V0_2.digest,
        candidate_set_digest=(
            dashboard_qam_candidate_set_digest_v0_2([])
            if candidate_count == 0
            else _SHA
        ),
        terminal_outcome=outcome,
        candidate_count=candidate_count,
    )
