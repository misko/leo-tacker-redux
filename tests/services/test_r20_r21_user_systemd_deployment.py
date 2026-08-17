from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNITS = ROOT / "deploy/gauss-main-r20-r21-v1/user-systemd"
CAPTURE = UNITS / "leo-v5-main-r20-r21-capture.service.in"
ANALYSIS = UNITS / "leo-v5-main-r20-r21-analysis.service.in"
ONLINE = UNITS / "leo-v5-main-r20-r21-online-analysis.service.in"
ONLINE_TIMER = UNITS / "leo-v5-main-r20-r21-online-analysis.timer.in"
RUNBOOK = ROOT / "docs/operations/gauss-main-user-systemd.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_user_units_bind_only_selected_release_receipt_and_fresh_main() -> None:
    for unit in (_text(CAPTURE), _text(ANALYSIS), _text(ONLINE)):
        assert "User=" not in unit
        assert "Group=" not in unit
        assert "@RELEASE_ROOT@/venv/bin/leo-v5-continuous" in unit
        assert "@RELEASE_ROOT@/config/runtime.json" in unit
        assert "@RELEASE_ROOT@/config/r20.station.json" in unit
        assert "@RELEASE_ROOT@/config/r21.station.json" in unit
        assert "@MAIN_DEFINITION_PATH@" in unit
        assert "@QUALIFICATION_RECEIPT_PATH@" in unit
        assert "@MAIN_DEFINITION_DIGEST@" in unit
        assert "@RELEASE_MANIFEST_SHA256@" in unit
        assert "@RELEASE_RECEIPT_SHA256@" in unit
        assert (
            "ExecStartPre=@RELEASE_ROOT@/venv/bin/leo-verify-release "
            "--manifest @RELEASE_ROOT@/release.manifest.json "
            "--receipt @RELEASE_ROOT@/validation.receipt.json "
            "--expected-manifest-sha256 @RELEASE_MANIFEST_SHA256@ "
            "--expected-receipt-sha256 @RELEASE_RECEIPT_SHA256@"
        ) in unit
        assert "@RELEASE_D_" not in unit
        assert "qual_gauss_r20_r21_20260816_v8/qualification.receipt.json" not in unit
        assert 'Environment="PYTHONNOUSERSITE=1"' in unit
        assert (
            'Environment="LD_LIBRARY_PATH=@RELEASE_ROOT@/native/lib:'
            '@RELEASE_ROOT@/python/lib"'
        ) in unit
        assert "/gits/leo-tracker-redux" not in unit
        assert "/.cache/" not in unit
        assert "qualification-v5" not in unit
        assert "192.168.1.17" not in unit
        assert "192.168.1.18" not in unit


def test_user_units_preserve_two_phase_bounds_and_fail_closed_restart() -> None:
    capture = _text(CAPTURE)
    analysis = _text(ANALYSIS)

    assert " capture-run " in capture
    assert " drain-analysis" not in capture
    assert "--maximum-transitions 1873" in capture
    assert "--maximum-runtime-seconds 32400" in capture
    assert "OnSuccess=leo-v5-main-r20-r21-analysis.service" in capture
    assert " drain-analysis-staged " in analysis
    assert " capture-run " not in analysis
    assert "--window-batches 36" in analysis
    assert "--compute-workers 8" in analysis
    assert "--projection-workers 4" in analysis
    assert "--maximum-transitions 27" in analysis
    assert "[Install]" in capture
    assert "[Install]" not in analysis
    for unit in (capture, analysis):
        assert "Restart=on-failure" in unit
        assert "RestartPreventExitStatus=2 3 4" in unit
        assert "RestartSec=1s" in unit
        assert "NoNewPrivileges=" not in unit
        assert "ProtectSystem=" not in unit
        assert "ProtectHome=" not in unit
        assert "CapabilityBoundingSet=" not in unit
        assert "AmbientCapabilities=" not in unit
        assert "UMask=0077" in unit


def test_capture_restart_delay_fits_reviewed_next_preflight_margin() -> None:
    period_s = 400 / 13
    preflight_s = 15
    slowest_requested_to_publication_s = 9.726
    restart_s = 1
    margin_s = period_s - preflight_s - slowest_requested_to_publication_s

    assert margin_s > 6.043
    assert restart_s < margin_s
    assert margin_s - restart_s > 5
    assert "RestartSec=1s" in _text(CAPTURE)


def test_online_analysis_is_bounded_separate_and_periodic() -> None:
    online = _text(ONLINE)
    timer = _text(ONLINE_TIMER)
    assert " drain-analysis-online " in online
    assert "--online-analysis-lock @MAIN_CAMPAIGN_ROOT@/online-analysis.lock" in online
    assert "--campaign-lock @MAIN_CAMPAIGN_ROOT@/campaign.lock" in online
    assert "--window-batches 36" in online
    assert "--compute-workers 8" in online
    assert "--projection-workers 4" in online
    assert "--maximum-transitions 27" in online
    assert "SuccessExitStatus=75" in online
    assert "MemoryMax=96G" in online
    assert "OnUnitInactiveSec=60s" in timer
    assert "AccuracySec=1s" in timer


def test_runbook_requires_linger_migration_and_offline_render_verification() -> None:
    runbook = _text(RUNBOOK)
    assert "0038_dashboard_surrogate_score_distributions.sql" in runbook
    assert "75,966,218,240" in runbook
    assert "Linger=yes" in runbook
    assert "systemd-analyze --user verify" in runbook
    assert "@RELEASE_MANIFEST_SHA256@" in runbook
    assert "@RELEASE_RECEIPT_SHA256@" in runbook
    assert "@MAIN_DEFINITION_PATH@" in runbook
    assert "@QUALIFICATION_RECEIPT_PATH@" in runbook
    assert "Do not compensate by running capture as root" in runbook
    assert "libiio" in runbook and "SPF" in runbook
