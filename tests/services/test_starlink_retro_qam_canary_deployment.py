from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from leo_flow.deployments.starlink_retro_qam_canary import main

MANIFEST = Path("tests/recording_analysis/fixtures/retro_qam_2026_08_17_v1.json")
DEPLOY = Path("deploy/starlink-retro-qam-canary-v1")


def test_timer_is_independent_bounded_and_periodic() -> None:
    service = (DEPLOY / "leo-starlink-retro-qam-canary.service.in").read_text()
    timer = (DEPLOY / "leo-starlink-retro-qam-canary.timer.in").read_text()
    assert "Type=oneshot" in service
    assert "leo-starlink-retro-qam-canary" in service
    assert (
        "ExecCondition=/usr/bin/flock --nonblock @CAPTURE_MODE_LOCK@ /usr/bin/true"
        in service
    )
    assert "RestrictAddressFamilies=AF_UNIX" in service
    assert "MemoryMax=4G" in service
    assert "OnUnitActiveSec=30min" in timer
    assert "Persistent=true" in timer
    for forbidden in ("leo-focused-continuous", "postgres", "radio", "iiod"):
        assert forbidden not in service.lower()


@pytest.mark.integration
def test_cli_writes_atomic_passing_receipt(tmp_path: Path, capsys) -> None:
    if not os.path.isdir("/mnt/qnap01/mouse9911/leo-store/2026_08_17_RETRO_QAM"):
        pytest.skip("read-only RETRO QAM corpus is not mounted")
    receipt = tmp_path / "latest.receipt.json"
    assert (
        main(
            [
                "--corpus-manifest",
                str(MANIFEST),
                "--receipt",
                str(receipt),
                "--git-commit",
                "0123456789abcdef",
            ]
        )
        == 0
    )
    document = json.loads(receipt.read_bytes())
    assert document["metrics_match_oracle"] is True
    assert document["candidate_only"] is True
    assert document["calibrated_detection"] is None
    assert document["combined"]["hard_symbol_accuracy"] == pytest.approx(
        0.88375, abs=2 / 2_400
    )
    assert not list(tmp_path.glob(".*.tmp"))
    assert json.loads(capsys.readouterr().out) == document
