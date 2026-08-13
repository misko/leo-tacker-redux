"""Versioned process configuration containing references, never secret values."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeAlias


class ConfigurationError(ValueError):
    """Configuration is malformed or from an unsupported schema version."""


@dataclass(frozen=True)
class SecretRef:
    provider: str
    name: str

    def __post_init__(self) -> None:
        if not self.provider or not self.name:
            raise ConfigurationError("secret references require provider and name")


@dataclass(frozen=True)
class RuntimeConfig:
    instance_id: str
    poll_interval_s: float
    shutdown_timeout_s: float
    secret_refs: tuple[SecretRef, ...]

    def __post_init__(self) -> None:
        if not self.instance_id:
            raise ConfigurationError("instance_id cannot be empty")
        if self.poll_interval_s <= 0 or self.shutdown_timeout_s <= 0:
            raise ConfigurationError("runtime intervals must be positive")


@dataclass(frozen=True)
class CaptureServiceConfig:
    schema_version: Literal[1]
    process: Literal["capture"]
    runtime: RuntimeConfig
    plan_source_ref: str
    radio_ref: str
    preflight_ref: str
    recording_writer_ref: str
    spool_ref: str
    recording_publisher_ref: str

    def __post_init__(self) -> None:
        _identity(self.schema_version, self.process, "capture")
        _refs(
            self.plan_source_ref,
            self.radio_ref,
            self.preflight_ref,
            self.recording_writer_ref,
            self.spool_ref,
            self.recording_publisher_ref,
        )


@dataclass(frozen=True)
class AnalysisServiceConfig:
    schema_version: Literal[1]
    process: Literal["analysis"]
    runtime: RuntimeConfig
    job_repository_ref: str
    recording_reader_ref: str
    feature_publisher_ref: str
    model_publisher_ref: str

    def __post_init__(self) -> None:
        _identity(self.schema_version, self.process, "analysis")
        _refs(
            self.job_repository_ref,
            self.recording_reader_ref,
            self.feature_publisher_ref,
            self.model_publisher_ref,
        )


@dataclass(frozen=True)
class DashboardServiceConfig:
    schema_version: Literal[1]
    process: Literal["dashboard"]
    runtime: RuntimeConfig
    query_projection_ref: str
    server_ref: str
    bind_host: str
    bind_port: int

    def __post_init__(self) -> None:
        _identity(self.schema_version, self.process, "dashboard")
        _refs(self.query_projection_ref, self.server_ref)
        if not self.bind_host or not 1 <= self.bind_port <= 65535:
            raise ConfigurationError("dashboard bind address is invalid")


ServiceConfig: TypeAlias = (
    CaptureServiceConfig | AnalysisServiceConfig | DashboardServiceConfig
)

_ROOT_KEYS = {"schema_version", "process", "runtime", "adapters"}
_RUNTIME_KEYS = {
    "instance_id",
    "poll_interval_s",
    "shutdown_timeout_s",
    "secret_refs",
}
_SECRET_KEYS = {"provider", "name"}
_ADAPTER_KEYS = {
    "capture": {
        "plan_source_ref",
        "radio_ref",
        "preflight_ref",
        "recording_writer_ref",
        "spool_ref",
        "recording_publisher_ref",
    },
    "analysis": {
        "job_repository_ref",
        "recording_reader_ref",
        "feature_publisher_ref",
        "model_publisher_ref",
    },
    "dashboard": {"query_projection_ref", "server_ref", "bind_host", "bind_port"},
}


def load_service_config(path: Path) -> ServiceConfig:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"cannot read configuration: {error}") from error
    return parse_service_config(value)


def parse_service_config(value: object) -> ServiceConfig:
    root = _mapping(value, "configuration")
    _exact_keys(root, _ROOT_KEYS, "configuration")
    if root["schema_version"] != 1:
        raise ConfigurationError("only configuration schema_version 1 is supported")
    process = root["process"]
    if process not in _ADAPTER_KEYS:
        raise ConfigurationError("process must be capture, analysis, or dashboard")
    runtime = _runtime(root["runtime"])
    adapters = _mapping(root["adapters"], "adapters")
    _exact_keys(adapters, _ADAPTER_KEYS[process], "adapters")
    refs = {
        key: _nonempty(adapters[key], key) for key in adapters if key.endswith("_ref")
    }
    if process == "capture":
        return CaptureServiceConfig(1, "capture", runtime, **refs)
    if process == "analysis":
        return AnalysisServiceConfig(1, "analysis", runtime, **refs)
    bind_port = adapters["bind_port"]
    if not isinstance(bind_port, int) or isinstance(bind_port, bool):
        raise ConfigurationError("bind_port must be an integer")
    return DashboardServiceConfig(
        1,
        "dashboard",
        runtime,
        refs["query_projection_ref"],
        refs["server_ref"],
        _nonempty(adapters["bind_host"], "bind_host"),
        bind_port,
    )


def _runtime(value: object) -> RuntimeConfig:
    item = _mapping(value, "runtime")
    _exact_keys(item, _RUNTIME_KEYS, "runtime")
    refs_value = item["secret_refs"]
    if not isinstance(refs_value, list):
        raise ConfigurationError("secret_refs must be a list")
    refs = []
    for index, raw in enumerate(refs_value):
        ref = _mapping(raw, f"secret_refs[{index}]")
        _exact_keys(ref, _SECRET_KEYS, f"secret_refs[{index}]")
        refs.append(
            SecretRef(
                _nonempty(ref["provider"], "provider"), _nonempty(ref["name"], "name")
            )
        )
    poll = _number(item["poll_interval_s"], "poll_interval_s")
    shutdown = _number(item["shutdown_timeout_s"], "shutdown_timeout_s")
    return RuntimeConfig(
        _nonempty(item["instance_id"], "instance_id"), poll, shutdown, tuple(refs)
    )


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigurationError(f"{label} must be an object with string keys")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ConfigurationError(
            f"{label} keys differ; missing={missing}, unknown={unknown}"
        )


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{label} must be a non-empty string")
    return value


def _number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigurationError(f"{label} must be a number")
    return float(value)


def _identity(version: object, process: object, expected_process: str) -> None:
    if version != 1 or process != expected_process:
        raise ConfigurationError("service configuration identity is invalid")


def _refs(*values: object) -> None:
    if any(not isinstance(value, str) or not value for value in values):
        raise ConfigurationError("adapter references must be non-empty strings")
