"""Canonical JSON codecs for radio lifecycle v0.1 facts.

The database adapter stores only this bounded public representation.  Raw
diagnostic responses and transport exceptions never cross this boundary.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from leo_flow.contracts.core import (
    V0_1,
    CaptureAttemptId,
    CaptureBatchId,
    RadioId,
    SchemaRef,
    SchemaVersion,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.radio_lifecycle import (
    Ad9361LifecycleIdentityV0_1,
    CaptureAttemptLifecycleFactV0_1,
    IiodProcessIdentityV0_1,
    RadioLifecycleConfidence,
    RadioLifecycleDiagnosisV0_1,
    RadioLifecycleObservationSource,
    RadioLifecycleObservationStatus,
    RadioLifecycleObservationV0_1,
    RadioLifecycleObserverUnavailableReason,
    RadioLifecycleReason,
    RadioLifecycleTrust,
    RadioTransportOutcome,
)


def encode_attempt_lifecycle_fact(fact: CaptureAttemptLifecycleFactV0_1) -> bytes:
    return canonical_json_bytes(fact)


def decode_attempt_lifecycle_fact(
    value: object,
) -> CaptureAttemptLifecycleFactV0_1:
    document = _document(value)
    return CaptureAttemptLifecycleFactV0_1(
        _schema(document["schema"]),
        CaptureBatchId(_string(document["batch_id"], "batch_id")),
        CaptureAttemptId(_string(document["attempt_id"], "attempt_id")),
        RadioId(_string(document["radio_id"], "radio_id")),
        _observation(document["preflight"]),
        _observation(document["terminal"]),
        RadioTransportOutcome(
            _string(document["transport_outcome"], "transport_outcome")
        ),
        _diagnosis(document["diagnosis"]),
    )


def _observation(value: object) -> RadioLifecycleObservationV0_1:
    item = _mapping(value, "observation")
    status = RadioLifecycleObservationStatus(
        _string(item["status"], "observation status")
    )
    unavailable = item.get("unavailable_reason")
    iiod = item.get("iiod")
    ad9361 = item.get("ad9361")
    return RadioLifecycleObservationV0_1(
        _schema(item["schema"]),
        RadioId(_string(item["radio_id"], "radio_id")),
        UtcNs(_integer(item["observed_utc_ns"], "observed_utc_ns")),
        status,
        RadioLifecycleObservationSource(_string(item["source"], "source")),
        RadioLifecycleTrust(_string(item["trust"], "trust")),
        _optional_string(item.get("boot_id"), "boot_id"),
        _optional_integer(item.get("uptime_ns"), "uptime_ns"),
        _optional_utc(item.get("estimated_boot_utc_ns"), "estimated_boot_utc_ns"),
        _optional_integer(
            item.get("boot_time_uncertainty_ns"), "boot_time_uncertainty_ns"
        ),
        None if iiod is None else _iiod(iiod),
        None if ad9361 is None else _ad9361(ad9361),
        None
        if unavailable is None
        else RadioLifecycleObserverUnavailableReason(
            _string(unavailable, "unavailable_reason")
        ),
    )


def _iiod(value: object) -> IiodProcessIdentityV0_1:
    item = _mapping(value, "iiod")
    return IiodProcessIdentityV0_1(
        _integer(item["pid"], "pid"),
        _integer(item["proc_start_ticks"], "proc_start_ticks"),
        _integer(item["clock_ticks_per_second"], "clock_ticks_per_second"),
    )


def _ad9361(value: object) -> Ad9361LifecycleIdentityV0_1:
    item = _mapping(value, "ad9361")
    return Ad9361LifecycleIdentityV0_1(
        _integer(item["initialization_epoch"], "initialization_epoch"),
        _optional_string(item.get("reset_reason"), "reset_reason"),
    )


def _diagnosis(value: object) -> RadioLifecycleDiagnosisV0_1:
    item = _mapping(value, "diagnosis")
    reason = item.get("reason")
    confidence = item.get("confidence")
    evidence = item.get("evidence_codes")
    if not isinstance(evidence, list):
        raise TypeError("evidence_codes must be an array")
    return RadioLifecycleDiagnosisV0_1(
        None if reason is None else RadioLifecycleReason(_string(reason, "reason")),
        None
        if confidence is None
        else RadioLifecycleConfidence(_string(confidence, "confidence")),
        tuple(_string(code, "evidence code") for code in evidence),
    )


def _schema(value: object) -> SchemaRef:
    item = _mapping(value, "schema")
    version = _mapping(item["version"], "schema version")
    return SchemaRef(
        _string(item["schema_id"], "schema_id"),
        SchemaVersion(
            _integer(version["major"], "schema major"),
            _integer(version["minor"], "schema minor"),
        ),
    )


def _document(value: object) -> Mapping[str, Any]:
    parsed: object
    if isinstance(value, (bytes, str)):
        parsed = json.loads(value)
    else:
        parsed = value
    result = _mapping(parsed, "lifecycle fact")
    schema = _schema(result["schema"])
    if schema != SchemaRef(CaptureAttemptLifecycleFactV0_1.SCHEMA_ID, V0_1):
        raise ValueError("unsupported attempt lifecycle fact schema")
    return result


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be an object")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def _optional_string(value: object, field: str) -> str | None:
    return None if value is None else _string(value, field)


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    return value


def _optional_integer(value: object, field: str) -> int | None:
    return None if value is None else _integer(value, field)


def _optional_utc(value: object, field: str) -> UtcNs | None:
    return None if value is None else UtcNs(_integer(value, field))
