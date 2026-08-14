"""Explicit-root filesystem capacity qualification for operators.

This maintenance command calls ``stat``/``statvfs`` for configured roots only.
It never walks a directory and is not part of a product-service workflow.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast

from leo_flow.contracts.core import canonical_json_bytes

_SCHEMA: Final = "org.leo-flow.storage-capacity/v1"
_RANK: Final = {"healthy": 0, "warn": 1, "critical": 2}


class CapacityConfigurationError(ValueError):
    """The closed capacity configuration is invalid or unreadable."""


class StatResult(Protocol):
    @property
    def st_dev(self) -> int: ...

    @property
    def st_ino(self) -> int: ...


class StatVfsResult(Protocol):
    @property
    def f_frsize(self) -> int: ...

    @property
    def f_blocks(self) -> int: ...

    @property
    def f_bavail(self) -> int: ...


@dataclass(frozen=True)
class CapacityThresholds:
    warn_free_bytes: int
    critical_free_bytes: int
    warn_free_fraction: float
    critical_free_fraction: float
    warn_seconds_to_full: float | None = None
    critical_seconds_to_full: float | None = None

    def __post_init__(self) -> None:
        if min(self.warn_free_bytes, self.critical_free_bytes) < 0:
            raise CapacityConfigurationError("free-byte thresholds cannot be negative")
        if self.critical_free_bytes > self.warn_free_bytes:
            raise CapacityConfigurationError("critical bytes must not exceed warning")
        if not 0 <= self.critical_free_fraction <= self.warn_free_fraction <= 1:
            raise CapacityConfigurationError("free-fraction thresholds are unordered")
        if (self.warn_seconds_to_full is None) != (
            self.critical_seconds_to_full is None
        ):
            raise CapacityConfigurationError("configure both time-to-full thresholds")
        if self.warn_seconds_to_full is not None:
            assert self.critical_seconds_to_full is not None
            if not 0 <= self.critical_seconds_to_full <= self.warn_seconds_to_full:
                raise CapacityConfigurationError(
                    "time-to-full thresholds are unordered"
                )


@dataclass(frozen=True)
class CapacityRoot:
    name: str
    path: Path
    estimated_bytes_per_second: float | None = None

    def __post_init__(self) -> None:
        if not self.name or any(character.isspace() for character in self.name):
            raise CapacityConfigurationError("root name must be a non-whitespace token")
        if not self.path.is_absolute():
            raise CapacityConfigurationError("root path must be absolute")
        rate = self.estimated_bytes_per_second
        if rate is not None and (rate < 0 or not math.isfinite(rate)):
            raise CapacityConfigurationError(
                "estimated rate must be finite and non-negative"
            )


@dataclass(frozen=True)
class CapacityConfiguration:
    thresholds: CapacityThresholds
    roots: tuple[CapacityRoot, ...]
    fail_on: str = "warn"

    def __post_init__(self) -> None:
        if not self.roots:
            raise CapacityConfigurationError("at least one root is required")
        if len({root.name for root in self.roots}) != len(self.roots):
            raise CapacityConfigurationError("root names must be unique")
        if self.fail_on not in {"warn", "critical"}:
            raise CapacityConfigurationError("fail_on must be warn or critical")


@dataclass(frozen=True)
class _Observation:
    root: CapacityRoot
    resolved_path: str
    device_id: int
    inode: int
    block_size: int
    total_bytes: int
    free_bytes: int


def load_configuration(path: Path) -> CapacityConfiguration:
    """Load a schema-v1 JSON configuration and reject unknown fields."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CapacityConfigurationError("configuration cannot be read") from error
    document = _object(raw, "configuration")
    _keys(document, {"schema_version", "fail_on", "thresholds", "roots"})
    if document.get("schema_version") != 1:
        raise CapacityConfigurationError("schema_version must be 1")
    fail_on = document.get("fail_on")
    if not isinstance(fail_on, str):
        raise CapacityConfigurationError("fail_on must be a string")
    raw_thresholds = _object(document.get("thresholds"), "thresholds")
    required = {
        "warn_free_bytes",
        "critical_free_bytes",
        "warn_free_fraction",
        "critical_free_fraction",
    }
    optional = {"warn_seconds_to_full", "critical_seconds_to_full"}
    _keys(raw_thresholds, required | optional)
    if not required <= raw_thresholds.keys():
        raise CapacityConfigurationError("threshold fields are missing")
    thresholds = CapacityThresholds(
        _integer(raw_thresholds["warn_free_bytes"]),
        _integer(raw_thresholds["critical_free_bytes"]),
        _number(raw_thresholds["warn_free_fraction"]),
        _number(raw_thresholds["critical_free_fraction"]),
        _optional_number(raw_thresholds.get("warn_seconds_to_full")),
        _optional_number(raw_thresholds.get("critical_seconds_to_full")),
    )
    raw_roots = document.get("roots")
    if not isinstance(raw_roots, list):
        raise CapacityConfigurationError("roots must be an array")
    roots: list[CapacityRoot] = []
    for value in raw_roots:
        root = _object(value, "root")
        _keys(root, {"name", "path", "estimated_bytes_per_second"})
        name, root_path = root.get("name"), root.get("path")
        if not isinstance(name, str) or not isinstance(root_path, str):
            raise CapacityConfigurationError("root name and path must be strings")
        roots.append(
            CapacityRoot(
                name,
                Path(root_path),
                _optional_number(root.get("estimated_bytes_per_second")),
            )
        )
    return CapacityConfiguration(thresholds, tuple(roots), fail_on)


def check_capacity(
    configuration: CapacityConfiguration,
    *,
    now_utc_ns: Callable[[], int],
    stat: Callable[[Path], StatResult] | None = None,
    statvfs: Callable[[Path], StatVfsResult] | None = None,
    resolve: Callable[[Path], Path] | None = None,
) -> dict[str, object]:
    """Observe configured roots only and return canonicalizable evidence."""

    checked_at = now_utc_ns()
    if checked_at < 0:
        raise ValueError("clock returned a negative timestamp")
    resolver = resolve or (lambda path: path.resolve(strict=True))
    stat_path = stat or _stat
    statvfs_path = statvfs or _statvfs
    observations: list[_Observation] = []
    reports: list[dict[str, object]] = []
    overall = "healthy"
    capacities: dict[int, tuple[int, int, int]] = {}
    for root in sorted(configuration.roots, key=lambda item: item.name):
        try:
            resolved = resolver(root.path)
            metadata = stat_path(resolved)
            capacity = capacities.get(metadata.st_dev)
            if capacity is None:
                filesystem = statvfs_path(resolved)
                if (
                    min(
                        filesystem.f_frsize,
                        filesystem.f_blocks,
                        filesystem.f_bavail,
                    )
                    < 0
                ):
                    raise OSError("negative capacity evidence")
                if filesystem.f_frsize == 0 or filesystem.f_blocks == 0:
                    raise OSError("capacity evidence unavailable")
                total = filesystem.f_frsize * filesystem.f_blocks
                free = min(total, filesystem.f_frsize * filesystem.f_bavail)
                capacity = (filesystem.f_frsize, total, free)
                capacities[metadata.st_dev] = capacity
            block_size, total, free = capacity
            observations.append(
                _Observation(
                    root,
                    str(resolved),
                    metadata.st_dev,
                    metadata.st_ino,
                    block_size,
                    total,
                    free,
                )
            )
        except (OSError, RuntimeError) as error:
            reports.append(
                {
                    "configured_path": str(root.path),
                    "name": root.name,
                    "reason": f"inaccessible:{type(error).__name__}",
                    "status": "critical",
                }
            )
            overall = "critical"

    by_device: dict[int, list[_Observation]] = {}
    for observation in observations:
        by_device.setdefault(observation.device_id, []).append(observation)
    for device_id, members in sorted(by_device.items()):
        members.sort(key=lambda item: item.root.name)
        first = members[0]
        aliases: dict[tuple[int, int], list[_Observation]] = {}
        for item in members:
            aliases.setdefault((item.device_id, item.inode), []).append(item)
        # Different configured roots on one filesystem are assumed independent.
        # Exact inode aliases contribute the conservative maximum rate only once.
        rate = sum(
            max(entry.root.estimated_bytes_per_second or 0.0 for entry in group)
            for group in aliases.values()
        )
        fraction = first.free_bytes / first.total_bytes
        seconds_to_full = first.free_bytes / rate if rate > 0 else None
        status, reasons = _classify(
            first.free_bytes, fraction, seconds_to_full, configuration.thresholds
        )
        overall = _worse(overall, status)
        for group in aliases.values():
            group.sort(key=lambda item: item.root.name)
            primary = group[0]
            for item in group:
                reports.append(
                    {
                        "block_size": item.block_size,
                        "configured_path": str(item.root.path),
                        "device_id": device_id,
                        "duplicate_of": None if item is primary else primary.root.name,
                        "estimated_filesystem_bytes_per_second": _rounded(rate),
                        "filesystem_free_bytes": item.free_bytes,
                        "filesystem_free_fraction": _rounded(fraction),
                        "filesystem_total_bytes": item.total_bytes,
                        "inode": item.inode,
                        "name": item.root.name,
                        "reasons": reasons,
                        "resolved_path": item.resolved_path,
                        "seconds_to_full": _rounded(seconds_to_full),
                        "status": status,
                    }
                )
    reports.sort(key=lambda item: cast(str, item["name"]))
    return {
        "checked_at_utc_ns": checked_at,
        "event": "storage_capacity_check",
        "overall_status": overall,
        "roots": reports,
        "schema": _SCHEMA,
    }


def exit_code(report: Mapping[str, object], fail_on: str) -> int:
    """Return zero or a monitoring-friendly warning/critical code."""

    status = report.get("overall_status")
    if status not in _RANK or fail_on not in {"warn", "critical"}:
        raise ValueError("unknown status or alert floor")
    if _RANK[status] < _RANK[fail_on]:
        return 0
    return 3 if status == "critical" else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="leo-flow-capacity")
    parser.add_argument("--config", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        configuration = load_configuration(arguments.config)
        report = check_capacity(configuration, now_utc_ns=time.time_ns)
    except CapacityConfigurationError:
        sys.stderr.write('{"event":"storage_capacity_configuration_failed"}\n')
        return 4
    sys.stdout.buffer.write(canonical_json_bytes(report) + b"\n")
    return exit_code(report, configuration.fail_on)


def _classify(
    free_bytes: int,
    free_fraction: float,
    seconds_to_full: float | None,
    thresholds: CapacityThresholds,
) -> tuple[str, list[str]]:
    critical: list[str] = []
    warning: list[str] = []
    if free_bytes <= thresholds.critical_free_bytes:
        critical.append("free-bytes-critical")
    elif free_bytes <= thresholds.warn_free_bytes:
        warning.append("free-bytes-warn")
    if free_fraction <= thresholds.critical_free_fraction:
        critical.append("free-fraction-critical")
    elif free_fraction <= thresholds.warn_free_fraction:
        warning.append("free-fraction-warn")
    if seconds_to_full is not None and thresholds.warn_seconds_to_full is not None:
        assert thresholds.critical_seconds_to_full is not None
        if seconds_to_full <= thresholds.critical_seconds_to_full:
            critical.append("time-to-full-critical")
        elif seconds_to_full <= thresholds.warn_seconds_to_full:
            warning.append("time-to-full-warn")
    reasons = sorted(critical or warning)
    return ("critical" if critical else "warn" if warning else "healthy", reasons)


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CapacityConfigurationError(f"{field} must be an object")
    return cast(dict[str, object], value)


def _keys(document: Mapping[str, object], allowed: set[str]) -> None:
    if document.keys() - allowed:
        raise CapacityConfigurationError("unknown configuration field")


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CapacityConfigurationError("expected an integer")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CapacityConfigurationError("expected a number")
    number = float(value)
    if number < 0 or not math.isfinite(number):
        raise CapacityConfigurationError("number must be finite and non-negative")
    return number


def _optional_number(value: object) -> float | None:
    return None if value is None else _number(value)


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 9)


def _worse(left: str, right: str) -> str:
    return left if _RANK[left] >= _RANK[right] else right


def _stat(path: Path) -> StatResult:
    return os.stat(path)


def _statvfs(path: Path) -> StatVfsResult:
    return os.statvfs(path)


if __name__ == "__main__":
    raise SystemExit(main())
