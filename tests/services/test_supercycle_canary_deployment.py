import json
from pathlib import Path

DEPLOY = Path("deploy/gauss-supercycle-canary-v1")


def test_canary_units_are_inactive_two_phase_and_state_isolated() -> None:
    capture = (DEPLOY / "leo-gauss-supercycle-canary-capture.service.in").read_text()
    analysis = (DEPLOY / "leo-gauss-supercycle-canary-analysis.service.in").read_text()
    assert "OnSuccess=leo-gauss-supercycle-canary-analysis.service" in capture
    assert " capture-run " in capture
    assert "--maximum-transitions 73" in capture
    assert " drain-analysis " in analysis
    assert "--maximum-transitions 37" in analysis
    assert "--analysis-deadline-seconds 21600" in analysis
    assert "/canary-supercycles/@CANARY_ID@" in capture
    assert "/canary-supercycles/@CANARY_ID@" in analysis
    assert "/continuous" not in capture + analysis
    assert "/var/lib/leo-flow/qualification" not in capture + analysis
    assert "Restart=no" in capture
    assert "Restart=no" in analysis
    assert "Type=exec" in capture
    assert "Type=exec" in analysis
    assert "Type=oneshot" not in capture + analysis
    assert "WantedBy=" not in analysis
    for unit in (capture, analysis):
        assert "User=" not in unit
        assert "@RELEASE_ROOT@/venv/bin/leo-v5-supercycle-canary" in unit
        assert "@RELEASE_ROOT@/native/lib" in unit
        assert "@RELEASE_MANIFEST_SHA256@" in unit
        assert "@RELEASE_RECEIPT_SHA256@" in unit
        assert "@CANARY_DEFINITION_DIGEST@" in unit
        assert (
            "/campaigns/qual_gauss_r20_r21_20260816_v8/qualification.receipt.json"
        ) in unit
        assert ("/canary-supercycles/@CANARY_ID@/definition.json") in unit
        assert (
            "ReadOnlyPaths=/home/mouse9911/.local/state/leo-flow/campaigns/"
            "qual_gauss_r20_r21_20260816_v8/qualification.receipt.json"
        ) in unit
        assert (
            "ReadOnlyPaths=/home/mouse9911/.local/state/leo-flow/"
            "canary-supercycles/@CANARY_ID@/definition.json"
        ) in unit
        assert "@RELEASE_D_" not in unit
        assert "@RELEASE_ROOT@/config/canary" not in unit
        assert "@RELEASE_ROOT@/config/qualification" not in unit
        assert "/RELEASE" not in unit
        assert "/.cache/" not in unit
    assert not (DEPLOY / "leo-gauss-supercycle-canary-capture@.service").exists()
    assert not (DEPLOY / "leo-gauss-supercycle-canary-analysis@.service").exists()


def test_canary_deployment_has_no_installed_live_identity() -> None:
    text = (DEPLOY / "README.md").read_text()
    assert "not installed or enabled" in text
    assert "main_campaign_authorized=false" in text
    assert "candidate-only" in text
    assert "detection count" in text
    schema = json.loads((DEPLOY / "definition.schema.json").read_text())
    properties = schema["properties"]
    assert schema["additionalProperties"] is False
    assert properties["slots"]["const"] == 36
    assert properties["recordings"]["const"] == 72
    assert properties["main_campaign_authorized"]["const"] is False
    assert properties["staged_analysis"]["const"]["compute_workers"] == 8
    assert properties["staged_analysis"]["const"]["projection_workers"] == 4
