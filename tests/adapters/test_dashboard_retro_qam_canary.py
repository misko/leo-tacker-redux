from __future__ import annotations

from pathlib import Path

import pytest

from leo_flow.adapters.dashboard_retro_qam_canary import (
    FileRetroQamCanaryDashboardQueryV0_1,
)
from leo_flow.contracts.core import canonical_json_bytes


def _document() -> dict[str, object]:
    receiver = lambda index, accuracy, evm: {
        "receiver_index": index,
        "winning_epoch_sample": 2063,
        "winning_cfo_hz": 364134.65 if index == 0 else -194373.48,
        "held_out_verify_score": 0.36,
        "conditioned_control_score": 0.02,
        "verify_minus_control_margin": 0.34,
        "hard_symbol_accuracy": accuracy,
        "rms_evm": evm,
    }
    return {
        "schema": {
            "schema_id": "org.leo-flow.starlink-retro-qam-canary-receipt",
            "version": {"major": 0, "minor": 1},
        },
        "corpus_id": "retro-positive",
        "iq_object_digest": {"algorithm": "sha256", "value": "a" * 64},
        "git_commit": "abc123",
        "completed_utc_ns": 1_787_026_764_761_437_071,
        "metrics_match_oracle": True,
        "candidate_only": True,
        "calibrated_detection": None,
        "reason_codes": [
            "known-published-pilot-regression",
            "candidate-evidence-not-calibrated-detection",
            "leo-tracker-oracle-not-runtime-dependency",
            "whole-input-sha256-verified-before-analysis",
        ],
        "receivers": [receiver(0, 0.748, 0.943), receiver(1, 0.799, 0.783)],
        "combined": {"hard_symbol_accuracy": 0.883, "rms_evm": 0.638},
    }


def _write(path: Path, document: dict[str, object]) -> None:
    path.write_bytes(canonical_json_bytes(document) + b"\n")


def test_reads_canonical_receipt_and_ranks_known_positive_high(tmp_path: Path) -> None:
    path = tmp_path / "latest.receipt.json"
    _write(path, _document())
    result = FileRetroQamCanaryDashboardQueryV0_1(path).latest_retro_qam_canary()
    assert result.metrics_match_oracle
    assert result.combined_qam_goodness > 0.8
    assert result.receivers[0].qam_goodness > 0.7
    assert result.receivers[1].qam_goodness > 0.7
    assert result.schedule_interval_seconds == 1800
    assert result.calibrated_detection is None


def test_rejects_noncanonical_or_unsafe_receipt(tmp_path: Path) -> None:
    path = tmp_path / "latest.receipt.json"
    document = _document()
    document["candidate_only"] = False
    _write(path, document)
    with pytest.raises(ValueError, match="safety semantics"):
        FileRetroQamCanaryDashboardQueryV0_1(path).latest_retro_qam_canary()
