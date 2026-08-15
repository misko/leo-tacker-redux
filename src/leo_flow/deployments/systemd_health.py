"""Bounded systemd health qualification for the three operational processes."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import TextIO

from leo_flow.contracts._validation import require_utc_ns
from leo_flow.contracts.core import Digest, UtcNs, canonical_digest

SCHEMA_ID = "org.leo-flow.systemd-health-config"
SCHEMA_VERSION = "0.1"
RECEIPT_SCHEMA_ID = "org.leo-flow.systemd-health-receipt"
RECEIPT_SCHEMA_VERSION = "0.1"
MAX_CONFIG_BYTES = 65_536
SYSTEMCTL_TIMEOUT_S = 5.0
_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\.(service|timer)$")
_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "NRestarts",
    "ExecMainStatus",
    "Restart",
)


class HealthQualificationError(RuntimeError):
    """Health configuration, observation, or receipt handling failed closed."""


class ServiceMode(str, Enum):
    RUNNING = "running"
    ONESHOT_COMPLETE = "oneshot-complete"


@dataclass(frozen=True)
class ServiceExpectation:
    component: str
    unit: str
    mode: ServiceMode
    restart: str
    maximum_restarts: int


@dataclass(frozen=True)
class HealthConfig:
    services: tuple[ServiceExpectation, ...]
    timers: tuple[str, ...]
    config_digest: Digest


@dataclass(frozen=True)
class UnitObservation:
    unit: str
    load_state: str
    active_state: str
    sub_state: str
    result: str
    restart_count: int | None
    exit_status: int | None
    restart: str
    passed: bool
    failures: tuple[str, ...]


UnitProbe = Callable[[str], Mapping[str, str]]


def load_config(path: Path) -> HealthConfig:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise HealthQualificationError("health config is unreadable") from error
    if len(payload) > MAX_CONFIG_BYTES:
        raise HealthQualificationError("health config exceeds its size bound")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HealthQualificationError("health config is not valid JSON") from error
    document = _mapping(value, "health config")
    _exact_keys(
        document,
        {"schema_id", "schema_version", "services", "timers"},
        "health config",
    )
    if document["schema_id"] != SCHEMA_ID or document["schema_version"] != (
        SCHEMA_VERSION
    ):
        raise HealthQualificationError("health config schema is unsupported")
    services = tuple(
        _service(item) for item in _sequence(document["services"], "services")
    )
    timers = tuple(
        _unit_name(item, "timer") for item in _sequence(document["timers"], "timers")
    )
    if not services or not timers:
        raise HealthQualificationError("health config requires services and timers")
    service_units = tuple(item.unit for item in services)
    if len({*service_units, *timers}) != len(service_units) + len(timers):
        raise HealthQualificationError("health unit names must be unique")
    components = [item.component for item in services]
    if set(components) != {"capture", "analysis", "dashboard"}:
        raise HealthQualificationError("capture, analysis, and dashboard are required")
    if components.count("capture") != 1 or components.count("dashboard") != 1:
        raise HealthQualificationError(
            "health config requires one capture and one dashboard service"
        )
    return HealthConfig(services, timers, canonical_digest(document))


def qualify_health(
    config: HealthConfig,
    probe: UnitProbe,
    observed_utc_ns: UtcNs,
) -> dict[str, object]:
    require_utc_ns(observed_utc_ns, "observed_utc_ns")
    observations = [
        _service_observation(expectation, probe(expectation.unit))
        for expectation in sorted(config.services, key=lambda item: item.unit)
    ]
    observations.extend(
        _timer_observation(unit, probe(unit)) for unit in sorted(config.timers)
    )
    observations.sort(key=lambda item: item.unit)
    passed = all(item.passed for item in observations)
    return {
        "schema_id": RECEIPT_SCHEMA_ID,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "observed_utc_ns": int(observed_utc_ns),
        "config_digest": str(config.config_digest),
        "status": "pass" if passed else "fail",
        "units": [asdict(item) for item in observations],
    }


def systemctl_probe(unit: str) -> Mapping[str, str]:
    command = [
        "/usr/bin/systemctl",
        "show",
        "--no-pager",
        f"--property={','.join(_PROPERTIES)}",
        unit,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HealthQualificationError("systemd health query failed") from error
    if result.returncode != 0:
        raise HealthQualificationError("systemd health query was rejected")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        name, separator, value = line.partition("=")
        if not separator or name not in _PROPERTIES or name in values:
            raise HealthQualificationError("systemd health response is invalid")
        values[name] = value
    if set(values) != set(_PROPERTIES):
        raise HealthQualificationError("systemd health response is incomplete")
    return values


def write_receipt(path: Path, document: Mapping[str, object]) -> None:
    if not path.is_absolute() or path == Path("/"):
        raise HealthQualificationError("health receipt path must be absolute")
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".health-", dir=path.parent)
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, payload + b"\n")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        raise HealthQualificationError("health receipt cannot be written") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    probe: UnitProbe = systemctl_probe,
    now_utc_ns: Callable[[], UtcNs] = lambda: UtcNs(time.time_ns()),
) -> int:
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    parser = argparse.ArgumentParser(prog="leo-flow-systemd-health")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        config = load_config(arguments.config)
        document = qualify_health(config, probe, now_utc_ns())
        write_receipt(arguments.receipt, document)
    except Exception:  # noqa: BLE001 - never expose process or path details.
        errors.write('{"event":"systemd_health_failed"}\n')
        errors.flush()
        return 3
    output.write(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    output.flush()
    return 0 if document["status"] == "pass" else 2


def _service(value: object) -> ServiceExpectation:
    item = _mapping(value, "service expectation")
    _exact_keys(
        item,
        {"component", "unit", "mode", "restart", "maximum_restarts"},
        "service expectation",
    )
    component = item["component"]
    if not isinstance(component, str) or component not in {
        "capture",
        "analysis",
        "dashboard",
    }:
        raise HealthQualificationError("service component is unsupported")
    unit = _unit_name(item["unit"], "service")
    try:
        mode = ServiceMode(item["mode"])
    except (TypeError, ValueError) as error:
        raise HealthQualificationError("service mode is unsupported") from error
    restart = item["restart"]
    if not isinstance(restart, str) or restart not in {
        "no",
        "on-failure",
        "always",
    }:
        raise HealthQualificationError("service restart policy is unsupported")
    maximum = item["maximum_restarts"]
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
        raise HealthQualificationError("maximum_restarts must be non-negative")
    return ServiceExpectation(component, unit, mode, restart, maximum)


def _service_observation(
    expected: ServiceExpectation, values: Mapping[str, str]
) -> UnitObservation:
    restart_count = _integer(values.get("NRestarts"), "NRestarts")
    exit_status = _integer(values.get("ExecMainStatus"), "ExecMainStatus")
    failures: list[str] = []
    if values.get("LoadState") != "loaded":
        failures.append("not_loaded")
    state = (values.get("ActiveState"), values.get("SubState"))
    accepted = (
        {("active", "running")}
        if expected.mode is ServiceMode.RUNNING
        else {("active", "exited"), ("inactive", "dead")}
    )
    if state not in accepted:
        failures.append("unexpected_state")
    if values.get("Result") != "success" or exit_status != 0:
        failures.append("unsuccessful_result")
    if values.get("Restart") != expected.restart:
        failures.append("restart_policy_changed")
    if restart_count > expected.maximum_restarts:
        failures.append("restart_limit_exceeded")
    return _observation(expected.unit, values, restart_count, exit_status, failures)


def _timer_observation(unit: str, values: Mapping[str, str]) -> UnitObservation:
    failures: list[str] = []
    if values.get("LoadState") != "loaded":
        failures.append("not_loaded")
    if (values.get("ActiveState"), values.get("SubState")) != (
        "active",
        "waiting",
    ):
        failures.append("unexpected_state")
    return _observation(unit, values, None, None, failures)


def _observation(
    unit: str,
    values: Mapping[str, str],
    restart_count: int | None,
    exit_status: int | None,
    failures: list[str],
) -> UnitObservation:
    return UnitObservation(
        unit,
        values.get("LoadState", ""),
        values.get("ActiveState", ""),
        values.get("SubState", ""),
        values.get("Result", ""),
        restart_count,
        exit_status,
        values.get("Restart", ""),
        not failures,
        tuple(failures),
    )


def _unit_name(value: object, kind: str) -> str:
    suffix = f".{kind}"
    if (
        not isinstance(value, str)
        or not _UNIT.fullmatch(value)
        or not value.endswith(suffix)
    ):
        raise HealthQualificationError(f"{kind} unit name is invalid")
    return value


def _integer(value: object, name: str) -> int:
    if not isinstance(value, str) or not value.isdigit():
        raise HealthQualificationError(f"systemd {name} is invalid")
    return int(value)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise HealthQualificationError(f"{name} must be an object")
    return value


def _sequence(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise HealthQualificationError(f"{name} must be an array")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise HealthQualificationError(f"{name} fields are not exact")


if __name__ == "__main__":
    raise SystemExit(main())
