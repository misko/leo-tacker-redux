from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from leo_flow.contracts.core import UtcNs
from leo_flow.deployments.systemd_health import (
    load_config,
    main,
    qualify_health,
    write_receipt,
)
from leo_flow.services.config import load_service_config

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
OPERATIONS = DEPLOY / "operations-v1"
OBSERVED_MONOTONIC_US = 1_000_000_000

UNIT_SOURCES = {
    "leo-flow.target": OPERATIONS / "leo-flow.target.example",
    "leo-flow-health.service": OPERATIONS / "leo-flow-health.service",
    "leo-flow-health.timer": OPERATIONS / "leo-flow-health.timer",
    "leo-v5-scan.service": DEPLOY / "v5-scan" / "leo-v5-scan.service",
    "leo-offline-analysis@.service": (
        DEPLOY / "offline-analysis-v1" / "leo-offline-analysis@.service.example"
    ),
    "leo-dashboard.service": DEPLOY / "dashboard-v1" / "leo-dashboard.service",
    "leo-storage-capacity.service": (
        DEPLOY / "storage-capacity" / "leo-storage-capacity.service"
    ),
    "leo-storage-capacity.timer": (
        DEPLOY / "storage-capacity" / "leo-storage-capacity.timer"
    ),
    "leo-ephemeris-provider-canary.service": (
        DEPLOY / "ephemeris-provider-canary" / "leo-ephemeris-provider-canary.service"
    ),
    "leo-ephemeris-provider-canary.timer": (
        DEPLOY / "ephemeris-provider-canary" / "leo-ephemeris-provider-canary.timer"
    ),
}


def _directives(path: Path) -> dict[tuple[str, str], list[str]]:
    section = ""
    result: dict[tuple[str, str], list[str]] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, separator, value = line.partition("=")
        assert separator and section, f"invalid unit line in {path.name}: {raw_line}"
        result.setdefault((section, key), []).append(value)
    return result


def _words(
    unit: Mapping[tuple[str, str], list[str]], section: str, key: str
) -> set[str]:
    return {word for value in unit.get((section, key), []) for word in value.split()}


def _healthy_properties(unit: str) -> dict[str, str]:
    if unit.endswith(".timer"):
        return {
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "waiting",
            "Result": "success",
            "NRestarts": "0",
            "ExecMainStatus": "0",
            "Restart": "no",
            "ExecMainStartTimestampMonotonic": "0",
            "ExecMainExitTimestampMonotonic": "0",
        }
    completed = {
        "leo-v5-scan.service",
        "leo-storage-capacity.service",
        "leo-ephemeris-provider-canary.service",
    }
    state = ("inactive", "dead") if unit in completed else ("active", "running")
    return {
        "LoadState": "loaded",
        "ActiveState": state[0],
        "SubState": state[1],
        "Result": "success",
        "NRestarts": "0",
        "ExecMainStatus": "0",
        "Restart": "no"
        if unit in completed - {"leo-v5-scan.service"}
        else "on-failure",
        "ExecMainStartTimestampMonotonic": "900000000",
        "ExecMainExitTimestampMonotonic": ("950000000" if unit in completed else "0"),
    }


def test_materialized_bundle_passes_isolated_systemd_verify(tmp_path: Path) -> None:
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd-analyze is not installed")

    materialized: list[Path] = []
    for unit_name, source in UNIT_SOURCES.items():
        destination = tmp_path / unit_name
        content = source.read_text(encoding="utf-8")
        # systemd-analyze also checks argv[0]. Substitute only the unavailable
        # packaged interpreter in the temporary rehearsal copy.
        content = content.replace(
            "ExecStart=/opt/leo-flow/bin/python",
            "ExecStart=/bin/true",
        )
        destination.write_text(content, encoding="utf-8")
        materialized.append(destination)

    vendor_paths = [
        path
        for path in (Path("/usr/lib/systemd/system"), Path("/lib/systemd/system"))
        if path.is_dir()
    ]
    environment = dict(os.environ)
    environment["SYSTEMD_UNIT_PATH"] = ":".join(
        [str(tmp_path), *(str(path) for path in vendor_paths)]
    )
    environment["SYSTEMD_COLORS"] = "0"
    environment["SYSTEMD_PAGER"] = "cat"
    result = subprocess.run(
        [analyzer, "verify", *(str(path) for path in materialized)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_exact_unit_graph_and_all_periodic_lanes_are_target_owned() -> None:
    units = {name: _directives(path) for name, path in UNIT_SOURCES.items()}
    target = units["leo-flow.target"]
    assert _words(target, "Unit", "Requires") == {"leo-storage-capacity.service"}
    assert _words(target, "Unit", "Wants") == {
        "network-online.target",
        "time-sync.target",
        "leo-v5-scan.service",
        "leo-offline-analysis@worker-1.service",
        "leo-dashboard.service",
        "leo-storage-capacity.timer",
        "leo-ephemeris-provider-canary.timer",
        "leo-flow-health.timer",
    }
    assert _words(target, "Unit", "After") == {
        "network-online.target",
        "time-sync.target",
        "leo-storage-capacity.service",
        "leo-v5-scan.service",
        "leo-offline-analysis@worker-1.service",
        "leo-dashboard.service",
    }

    assert _words(units["leo-v5-scan.service"], "Unit", "After") == {
        "network-online.target",
        "time-sync.target",
        "leo-storage-capacity.service",
    }
    assert _words(units["leo-offline-analysis@.service"], "Unit", "After") == {
        "network-online.target",
        "time-sync.target",
        "leo-storage-capacity.service",
        "leo-v5-scan.service",
    }
    assert _words(units["leo-dashboard.service"], "Unit", "After") == {
        "network-online.target",
        "leo-offline-analysis@worker-1.service",
    }
    assert "leo-v5-scan.service" not in _words(
        units["leo-offline-analysis@.service"], "Unit", "Requires"
    )
    assert "leo-offline-analysis@worker-1.service" not in _words(
        units["leo-dashboard.service"], "Unit", "Requires"
    )

    target_owned = {
        name
        for name, unit in units.items()
        if "leo-flow.target" in _words(unit, "Unit", "PartOf")
    }
    assert target_owned == set(UNIT_SOURCES) - {"leo-flow.target"}

    timer_services = {
        name: next(iter(_words(unit, "Timer", "Unit")))
        for name, unit in units.items()
        if name.endswith(".timer")
    }
    assert timer_services == {
        "leo-flow-health.timer": "leo-flow-health.service",
        "leo-storage-capacity.timer": "leo-storage-capacity.service",
        "leo-ephemeris-provider-canary.timer": (
            "leo-ephemeris-provider-canary.service"
        ),
    }
    assert set(load_config(OPERATIONS / "health.example.json").timers) == set(
        timer_services
    )
    ephemeris_service = units["leo-ephemeris-provider-canary.service"]
    assert ephemeris_service[("Service", "RestrictAddressFamilies")] == ["AF_UNIX"]
    assert "--allow-network" not in ephemeris_service[("Service", "ExecStart")][0]


def test_workers_locks_and_credentials_are_instance_or_role_scoped() -> None:
    component_names = (
        "leo-v5-scan.service",
        "leo-offline-analysis@.service",
        "leo-dashboard.service",
    )
    components = {name: _directives(UNIT_SOURCES[name]) for name in component_names}
    credentials = {
        name: components[name][("Service", "LoadCredential")][0]
        for name in component_names
    }
    assert credentials == {
        "leo-v5-scan.service": (
            "catalog-dsn:/etc/leo-flow/secrets/capture-catalog-dsn"
        ),
        "leo-offline-analysis@.service": (
            "catalog-dsn:/etc/leo-flow/secrets/analysis-catalog-dsn"
        ),
        "leo-dashboard.service": (
            "catalog-dsn:/etc/leo-flow/secrets/dashboard-catalog-dsn"
        ),
    }
    assert len(set(credentials.values())) == len(credentials)

    for unit in components.values():
        command = unit[("Service", "ExecStart")][0]
        assert command.startswith("/usr/bin/flock --nonblock /run/")
        assert " sh " not in f" {command} "
        assert unit[("Service", "DynamicUser")] == ["yes"]
        assert ("Service", "Environment") not in unit

    template = UNIT_SOURCES["leo-offline-analysis@.service"].read_text()
    worker_one = template.replace("%i", "worker-1")
    worker_two = template.replace("%i", "worker-2")
    for marker in (
        "StateDirectory=",
        "RuntimeDirectory=",
        "/worker.lock",
        "--config /etc/leo-flow/analysis-",
    ):
        first = next(line for line in worker_one.splitlines() if marker in line)
        second = next(line for line in worker_two.splitlines() if marker in line)
        assert first != second


def test_shutdown_timeout_and_restart_contract_match_checked_configs() -> None:
    cases = (
        ("leo-v5-scan.service", DEPLOY / "v5-scan" / "capture.json", "SIGINT"),
        (
            "leo-offline-analysis@.service",
            DEPLOY / "offline-analysis-v1" / "analysis.json",
            "SIGTERM",
        ),
        (
            "leo-dashboard.service",
            DEPLOY / "dashboard-v1" / "dashboard.json",
            "SIGTERM",
        ),
    )
    for unit_name, config_path, signal in cases:
        unit = _directives(UNIT_SOURCES[unit_name])
        config = load_service_config(config_path)
        stop_timeout = float(unit[("Service", "TimeoutStopSec")][0].removesuffix("s"))
        assert stop_timeout > config.runtime.shutdown_timeout_s
        assert unit[("Service", "KillSignal")] == [signal]
        assert unit[("Service", "Restart")] == ["on-failure"]
        assert unit[("Unit", "StartLimitBurst")] == ["5"]
        assert ("Service", "ExecStopPost") not in unit


def test_synthetic_lifecycle_properties_and_receipt_preservation(
    tmp_path: Path,
) -> None:
    config_value = json.loads((OPERATIONS / "health.example.json").read_text())
    second_worker = dict(config_value["services"][1])
    second_worker["unit"] = "leo-offline-analysis@worker-2.service"
    config_value["services"].append(second_worker)
    config_path = tmp_path / "health.json"
    config_path.write_text(json.dumps(config_value), encoding="utf-8")
    config = load_config(config_path)
    states = {
        unit: _healthy_properties(unit)
        for unit in (
            *(service.unit for service in config.services),
            *config.timers,
        )
    }

    def probe(unit: str) -> Mapping[str, str]:
        return states[unit]

    assert (
        qualify_health(config, probe, UtcNs(1), OBSERVED_MONOTONIC_US)["status"]
        == "pass"
    )

    worker = states["leo-offline-analysis@worker-2.service"]
    worker.update(
        ActiveState="failed",
        SubState="failed",
        Result="exit-code",
        ExecMainStatus="1",
        NRestarts="1",
    )
    assert (
        qualify_health(config, probe, UtcNs(2), OBSERVED_MONOTONIC_US)["status"]
        == "fail"
    )

    worker.update(
        ActiveState="active",
        SubState="running",
        Result="success",
        ExecMainStatus="0",
    )
    assert (
        qualify_health(config, probe, UtcNs(3), OBSERVED_MONOTONIC_US)["status"]
        == "pass"
    )

    worker.update(
        ActiveState="failed",
        SubState="failed",
        Result="start-limit-hit",
        ExecMainStatus="1",
        NRestarts="6",
    )
    latest = tmp_path / "receipts" / "latest.json"
    assert (
        main(
            ["--config", str(config_path), "--receipt", str(latest)],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            probe=probe,
            now_utc_ns=lambda: UtcNs(4),
            now_monotonic_us=lambda: OBSERVED_MONOTONIC_US,
        )
        == 2
    )
    failed_evidence = latest.read_bytes()

    def unavailable(_unit: str) -> Mapping[str, str]:
        raise OSError("rehearsal probe unavailable")

    assert (
        main(
            ["--config", str(config_path), "--receipt", str(latest)],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            probe=unavailable,
            now_utc_ns=lambda: UtcNs(5),
            now_monotonic_us=lambda: OBSERVED_MONOTONIC_US,
        )
        == 3
    )
    assert latest.read_bytes() == failed_evidence

    archived = tmp_path / "receipts" / "incident-before-reset.json"
    write_receipt(archived, json.loads(failed_evidence))
    assert archived.stat().st_mode & 0o777 == 0o600
    # Model the property values expected after reset-failed and a clean restart.
    worker.update(
        ActiveState="active",
        SubState="running",
        Result="success",
        ExecMainStatus="0",
        NRestarts="0",
    )
    assert (
        main(
            ["--config", str(config_path), "--receipt", str(latest)],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            probe=probe,
            now_utc_ns=lambda: UtcNs(6),
            now_monotonic_us=lambda: OBSERVED_MONOTONIC_US,
        )
        == 0
    )
    assert json.loads(latest.read_text())["status"] == "pass"
    assert archived.read_bytes() == failed_evidence
