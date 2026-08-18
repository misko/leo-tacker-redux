"""Capture-independent admission contract for optional heavy analysis work."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class FocusedCaptureGuardV0_1:
    """Small local-runtime snapshot published by the focused supervisor."""

    observed_utc_ns: int
    valid_until_utc_ns: int
    capture_guard_from_utc_ns: int
    capture_guard_until_utc_ns: int
    focused_backlog: int
    in_flight_analyses: int
    continuous_capture_active: bool

    SCHEMA_ID = "org.leo-flow.focused-capture-heavy-work-guard/v0.1"

    def __post_init__(self) -> None:
        values = (
            self.observed_utc_ns,
            self.valid_until_utc_ns,
            self.capture_guard_from_utc_ns,
            self.capture_guard_until_utc_ns,
            self.focused_backlog,
            self.in_flight_analyses,
        )
        if any(value < 0 for value in values):
            raise ValueError("capture guard values must be non-negative")
        if self.valid_until_utc_ns < self.observed_utc_ns:
            raise ValueError("capture guard validity precedes observation")
        if self.capture_guard_until_utc_ns < self.capture_guard_from_utc_ns:
            raise ValueError("capture guard interval is reversed")


@dataclass(frozen=True, slots=True)
class HeavyWorkResourceSnapshotV0_1:
    observed_utc_ns: int
    cpu_count: int
    load_1m: float
    memory_available_bytes: int
    io_pressure_avg10: float

    def __post_init__(self) -> None:
        if (
            self.observed_utc_ns < 0
            or self.cpu_count < 1
            or self.load_1m < 0
            or self.memory_available_bytes < 0
            or self.io_pressure_avg10 < 0
        ):
            raise ValueError("resource snapshot is invalid")


@dataclass(frozen=True, slots=True)
class HeavyWorkAdmissionDecisionV0_1:
    admitted: bool
    reason: str


class OptionalHeavyWorkAdmissionPortV0_1(Protocol):
    def acquire(
        self,
    ) -> tuple[HeavyWorkAdmissionDecisionV0_1, HeavyWorkAdmissionPermitV0_1 | None]: ...


class HeavyWorkAdmissionPermitV0_1(Protocol):
    def release(self) -> None: ...


def encode_focused_capture_guard_v0_1(snapshot: FocusedCaptureGuardV0_1) -> bytes:
    return (
        json.dumps(
            {
                "capture_guard_until_utc_ns": snapshot.capture_guard_until_utc_ns,
                "capture_guard_from_utc_ns": snapshot.capture_guard_from_utc_ns,
                "continuous_capture_active": snapshot.continuous_capture_active,
                "focused_backlog": snapshot.focused_backlog,
                "in_flight_analyses": snapshot.in_flight_analyses,
                "observed_utc_ns": snapshot.observed_utc_ns,
                "schema": FocusedCaptureGuardV0_1.SCHEMA_ID,
                "valid_until_utc_ns": snapshot.valid_until_utc_ns,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def decode_focused_capture_guard_v0_1(payload: bytes) -> FocusedCaptureGuardV0_1:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("capture guard is not valid JSON") from error
    if not isinstance(document, dict) or set(document) != {
        "capture_guard_until_utc_ns",
        "capture_guard_from_utc_ns",
        "continuous_capture_active",
        "focused_backlog",
        "in_flight_analyses",
        "observed_utc_ns",
        "schema",
        "valid_until_utc_ns",
    }:
        raise ValueError("capture guard fields differ from the contract")
    if document["schema"] != FocusedCaptureGuardV0_1.SCHEMA_ID:
        raise ValueError("capture guard schema differs")
    if not isinstance(document["continuous_capture_active"], bool):
        raise TypeError("capture guard active flag is invalid")
    integers = (
        "capture_guard_until_utc_ns",
        "capture_guard_from_utc_ns",
        "focused_backlog",
        "in_flight_analyses",
        "observed_utc_ns",
        "valid_until_utc_ns",
    )
    if any(type(document[name]) is not int for name in integers):
        raise ValueError("capture guard integer field is invalid")
    snapshot = FocusedCaptureGuardV0_1(
        int(document["observed_utc_ns"]),
        int(document["valid_until_utc_ns"]),
        int(document["capture_guard_from_utc_ns"]),
        int(document["capture_guard_until_utc_ns"]),
        int(document["focused_backlog"]),
        int(document["in_flight_analyses"]),
        bool(document["continuous_capture_active"]),
    )
    if encode_focused_capture_guard_v0_1(snapshot) != payload:
        raise ValueError("capture guard is not canonical")
    return snapshot
