from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.contracts.core import UtcNs
from leo_flow.deployments.systemd_health import (
    HealthQualificationError,
    load_config,
    main,
    qualify_health,
)
from leo_flow.services.config import load_service_config

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
OPERATIONS = DEPLOY / "operations-v1"
HEALTH_CONFIG = OPERATIONS / "health.example.json"


def _properties(
    *,
    active: str,
    sub: str,
    restart: str = "on-failure",
    restarts: int = 0,
) -> dict[str, str]:
    return {
        "LoadState": "loaded",
        "ActiveState": active,
        "SubState": sub,
        "Result": "success",
        "NRestarts": str(restarts),
        "ExecMainStatus": "0",
        "Restart": restart,
    }


def _healthy_probe(unit: str) -> dict[str, str]:
    if unit.endswith(".timer"):
        return _properties(active="active", sub="waiting", restart="no")
    if unit == "leo-v5-scan.service":
        return _properties(active="inactive", sub="dead")
    return _properties(active="active", sub="running")


def test_checked_configs_use_strict_parsers_and_matching_health_schema() -> None:
    assert load_service_config(DEPLOY / "v5-scan" / "capture.json").process == (
        "capture"
    )
    assert (
        load_service_config(DEPLOY / "offline-analysis-v1" / "analysis.json").process
        == "analysis"
    )
    assert load_service_config(DEPLOY / "dashboard-v1" / "dashboard.json").process == (
        "dashboard"
    )
    config = load_config(HEALTH_CONFIG)
    assert {item.component for item in config.services} == {
        "capture",
        "analysis",
        "dashboard",
    }
    schema = json.loads((OPERATIONS / "health.schema.json").read_text())
    assert schema["properties"]["schema_id"]["const"] == (
        "org.leo-flow.systemd-health-config"
    )
    assert schema["additionalProperties"] is False


def test_health_receipt_is_deterministic_and_covers_restarts_and_timers() -> None:
    config = load_config(HEALTH_CONFIG)
    first = qualify_health(config, _healthy_probe, UtcNs(123))
    second = qualify_health(config, _healthy_probe, UtcNs(123))

    assert first == second
    assert first["status"] == "pass"
    units = first["units"]
    assert isinstance(units, list)
    assert [item["unit"] for item in units] == sorted(item["unit"] for item in units)
    assert {item["unit"] for item in units} == {
        "leo-v5-scan.service",
        "leo-offline-analysis@worker-1.service",
        "leo-dashboard.service",
        "leo-flow-health.timer",
        "leo-storage-capacity.timer",
    }

    failed = replace(
        config,
        services=tuple(
            replace(item, maximum_restarts=0) if item.component == "dashboard" else item
            for item in config.services
        ),
    )

    def restarted(unit: str) -> dict[str, str]:
        values = _healthy_probe(unit)
        if unit == "leo-dashboard.service":
            values["NRestarts"] = "1"
        return values

    receipt = qualify_health(failed, restarted, UtcNs(124))
    assert receipt["status"] == "fail"
    receipt_units = receipt["units"]
    assert isinstance(receipt_units, list)
    dashboard = next(
        item for item in receipt_units if item["unit"] == "leo-dashboard.service"
    )
    assert dashboard["failures"] == ("restart_limit_exceeded",)


def test_health_config_rejects_unknown_fields_and_missing_component(
    tmp_path: Path,
) -> None:
    value = json.loads(HEALTH_CONFIG.read_text())
    value["ambient_discovery"] = True
    path = tmp_path / "unknown.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(HealthQualificationError, match="fields are not exact"):
        load_config(path)

    value.pop("ambient_discovery")
    value["services"] = [
        item for item in value["services"] if item["component"] != "dashboard"
    ]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(HealthQualificationError, match="dashboard"):
        load_config(path)

    value = json.loads(HEALTH_CONFIG.read_text())
    second_capture = dict(value["services"][0])
    second_capture["unit"] = "leo-second-capture.service"
    value["services"].append(second_capture)
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(HealthQualificationError, match="one capture"):
        load_config(path)


def test_cli_writes_one_atomic_operator_receipt_without_live_io(tmp_path: Path) -> None:
    receipt = tmp_path / "state" / "latest.json"
    stdout = io.StringIO()
    assert (
        main(
            ["--config", str(HEALTH_CONFIG), "--receipt", str(receipt)],
            stdout=stdout,
            stderr=io.StringIO(),
            probe=_healthy_probe,
            now_utc_ns=lambda: UtcNs(456),
        )
        == 0
    )
    assert json.loads(stdout.getvalue()) == json.loads(receipt.read_text())
    assert receipt.stat().st_mode & 0o777 == 0o600


def test_cli_persists_failed_qualification_and_sanitizes_probe_errors(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "failed.json"

    def unhealthy(unit: str) -> dict[str, str]:
        values = _healthy_probe(unit)
        if unit == "leo-dashboard.service":
            values["ActiveState"] = "failed"
            values["SubState"] = "failed"
        return values

    assert (
        main(
            ["--config", str(HEALTH_CONFIG), "--receipt", str(receipt)],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            probe=unhealthy,
            now_utc_ns=lambda: UtcNs(789),
        )
        == 2
    )
    assert json.loads(receipt.read_text())["status"] == "fail"

    errors = io.StringIO()

    def rejected_probe(_unit: str) -> dict[str, str]:
        raise HealthQualificationError("sensitive detail")

    assert (
        main(
            [
                "--config",
                str(HEALTH_CONFIG),
                "--receipt",
                str(tmp_path / "unwritten.json"),
            ],
            stdout=io.StringIO(),
            stderr=errors,
            probe=rejected_probe,
            now_utc_ns=lambda: UtcNs(790),
        )
        == 3
    )
    assert errors.getvalue() == '{"event":"systemd_health_failed"}\n'
    assert not (tmp_path / "unwritten.json").exists()


def test_systemd_bundle_orders_components_without_runtime_coupling() -> None:
    capture = (DEPLOY / "v5-scan" / "leo-v5-scan.service").read_text()
    analysis = (
        DEPLOY / "offline-analysis-v1" / "leo-offline-analysis@.service.example"
    ).read_text()
    dashboard = (DEPLOY / "dashboard-v1" / "leo-dashboard.service").read_text()
    target = (OPERATIONS / "leo-flow.target.example").read_text()
    health = (OPERATIONS / "leo-flow-health.service").read_text()

    assert "After=leo-storage-capacity.service" in capture
    assert (
        "After=network-online.target time-sync.target leo-storage-capacity.service leo-v5-scan.service"
        in analysis
    )
    assert "Before=leo-dashboard.service" in analysis
    assert (
        "After=network-online.target leo-offline-analysis@worker-1.service" in dashboard
    )
    assert "Requires=leo-storage-capacity.service" in target
    assert (
        "leo-v5-scan.service leo-offline-analysis@worker-1.service leo-dashboard.service"
        in target
    )
    assert "leo_flow.deployments.systemd_health" in health
    for unit in (capture, analysis, dashboard, health):
        assert "/usr/bin/flock --nonblock" in unit
        assert "Restart=always" not in unit
        assert "sh -c" not in unit
    assert "leo_flow.analysis" not in capture
    assert "leo_flow.capture" not in analysis


def test_periodic_health_and_capacity_timers_coexist_without_shared_work() -> None:
    health = (OPERATIONS / "leo-flow-health.timer").read_text()
    capacity = (DEPLOY / "storage-capacity" / "leo-storage-capacity.timer").read_text()
    assert "Unit=leo-flow-health.service" in health
    assert "Unit=leo-storage-capacity.service" in capacity
    assert "Persistent=true" in health
    assert "Persistent=true" in capacity
    assert "Conflicts=" not in health + capacity
