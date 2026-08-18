from __future__ import annotations

import io
from pathlib import Path

from leo_flow.deployments import retro_qam_recording_import as deployment


def _arguments(tmp_path: Path) -> list[str]:
    return [
        "--corpus-manifest",
        str(tmp_path / "corpus.json"),
        "--archive-root",
        str(tmp_path / "archive"),
        "--expected-manifest-sha256",
        "a" * 64,
        "--staging-root",
        str(tmp_path / "staging"),
        "--cas-root",
        str(tmp_path / "cas"),
        "--capture-credential-directory",
        str(tmp_path / "capture-credentials"),
        "--analysis-credential-directory",
        str(tmp_path / "analysis-credentials"),
    ]


def test_one_shot_cli_emits_public_historical_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    observed = None

    def execute(args):
        nonlocal observed
        observed = args
        return {
            "event": "retro_qam_recording_imported",
            "recording_id": "rec_retro_qam_20260813_clip002",
            "historical_capture": True,
            "conditioned_canary": True,
            "calibrated_detection": False,
            "calibration_eligible": False,
        }

    monkeypatch.setattr(deployment, "execute_import", execute)
    stdout, stderr = io.StringIO(), io.StringIO()

    assert deployment.main(_arguments(tmp_path), stdout=stdout, stderr=stderr) == 0
    assert stderr.getvalue() == ""
    assert '"recording_id":"rec_retro_qam_20260813_clip002"' in stdout.getvalue()
    assert observed.archive_root == tmp_path / "archive"
    assert observed.dashboard_base_url == "http://gauss:8090"


def test_one_shot_cli_fails_closed_without_leaking_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    def fail(_args):
        raise deployment.RetroQamImportDeploymentError("publication conflict")

    monkeypatch.setattr(deployment, "execute_import", fail)
    stdout, stderr = io.StringIO(), io.StringIO()

    assert deployment.main(_arguments(tmp_path), stdout=stdout, stderr=stderr) == 2
    assert stdout.getvalue() == ""
    assert "publication conflict" in stderr.getvalue()
    assert "capture-credentials" not in stderr.getvalue()


def test_console_script_is_registered() -> None:
    project = Path("pyproject.toml").read_text(encoding="utf-8")
    assert (
        'leo-retro-qam-recording-import = '
        '"leo_flow.deployments.retro_qam_recording_import:main"'
    ) in project
