import json
from pathlib import Path

DEPLOY = Path("deploy/gauss-symbolwise-replay-v1")


def test_symbolwise_worker_is_optional_single_claim_and_capture_aware() -> None:
    unit = (DEPLOY / "leo-gauss-symbolwise-replay.service.in").read_text()
    config = json.loads((DEPLOY / "deployment.json").read_bytes())
    assert config["required_migration"].startswith("0052_")
    assert config["enabled_by_default"] is False
    assert config["admission"] == {
        "mode": "explicit-request-only",
        "automatic_capture_enqueue": False,
        "maximum_claims_per_cycle": 1,
        "maximum_optional_concurrency": 1,
        "maximum_focused_backlog": 0,
        "host_cpu_cores": 24,
        "reserved_cpu_cores": 8,
        "estimated_claim_cpu_cores": 3,
        "minimum_memory_available_bytes": 8589934592,
        "maximum_io_pressure_avg10": 5.0,
    }
    for required in (
        "leo-gauss-symbolwise-replay",
        "ExecStartPre=@RELEASE_ROOT@/venv/bin/leo-verify-release",
        "--capture-guard-status %t/leo-flow-optional-heavy/guard.json",
        "--maximum-focused-backlog 0",
        "--host-cpu-cores 24",
        "--reserved-cpu-cores 8",
        "--estimated-claim-cpu-cores 3",
        "--minimum-memory-available-bytes 8589934592",
        "--maximum-io-pressure-avg10 5",
        "--maximum-optional-concurrency 1",
        "CPUQuota=300%",
        "MemoryMax=12G",
        "TasksMax=16",
        "Nice=15",
    ):
        assert required in unit
    for forbidden in ("enqueue", "capture.service", "DeviceAllow"):
        assert forbidden not in unit


def test_rollout_keeps_enqueue_explicit_and_readiness_offline_first() -> None:
    runbook = (DEPLOY / "README.md").read_text()
    validate = runbook.index("validate-request")
    enqueue = runbook.index("enqueue-request")
    start = runbook.index("Start it manually")
    assert validate < enqueue < start
    for required in (
        "no capture is active",
        "one reviewed exact request at a time",
        "never add capture-triggered admission",
        "systemd-analyze --user verify",
    ):
        assert required in runbook
