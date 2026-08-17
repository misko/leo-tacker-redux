"""Strict canonical JSON codec for the public capture-batch contracts."""

from __future__ import annotations

import json
from typing import Any, NoReturn, cast

from .capture_batch import (
    CaptureAttemptOutcome,
    CaptureAttemptState,
    CaptureBatchDefinition,
    CaptureBatchMode,
    CaptureBatchSnapshot,
    ExpectedCaptureAttempt,
)
from .core import (
    CaptureAttemptId,
    CaptureBatchId,
    Digest,
    DigestAlgorithm,
    PlanId,
    RadioId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    UtcNs,
    canonical_json_bytes,
)
from .storage import ObjectRef, PublishedRecordingRef, RecordingObjectRef


class CaptureBatchDocumentError(ValueError):
    """A public batch document is malformed, non-canonical, or unsupported."""


def encode_capture_batch_definition(definition: CaptureBatchDefinition) -> bytes:
    return canonical_json_bytes(definition)


def encode_capture_batch_snapshot(snapshot: CaptureBatchSnapshot) -> bytes:
    return canonical_json_bytes(snapshot)


def decode_capture_batch_definition(encoded: bytes) -> CaptureBatchDefinition:
    value = _document(encoded)
    _fields(
        value,
        {
            "schema",
            "batch_id",
            "mode",
            "expected_attempts",
            "maximum_observed_start_skew_ns",
        },
        "batch definition",
    )
    attempts = _list(value["expected_attempts"], "expected_attempts")
    if len(attempts) != 2:
        _bad("batch requires exactly two attempts")
    result = CaptureBatchDefinition(
        _schema(value["schema"]),
        CaptureBatchId(_string(value["batch_id"], "batch_id")),
        CaptureBatchMode(_string(value["mode"], "mode")),
        (_expected(attempts[0]), _expected(attempts[1])),
        _optional_int(
            value["maximum_observed_start_skew_ns"],
            "maximum_observed_start_skew_ns",
        ),
    )
    if encode_capture_batch_definition(result) != encoded:
        _bad("batch definition is not canonical")
    return result


def decode_capture_batch_snapshot(encoded: bytes) -> CaptureBatchSnapshot:
    value = _document(encoded)
    _fields(value, {"schema", "definition", "outcomes", "revision"}, "snapshot")
    definition = decode_capture_batch_definition(
        canonical_json_bytes(value["definition"])
    )
    result = CaptureBatchSnapshot(
        _schema(value["schema"]),
        definition,
        tuple(_outcome(item) for item in _list(value["outcomes"], "outcomes")),
        _integer(value["revision"], "revision"),
    )
    if encode_capture_batch_snapshot(result) != encoded:
        _bad("batch snapshot is not canonical")
    return result


def _expected(value: object) -> ExpectedCaptureAttempt:
    item = _object(value, "expected attempt")
    _fields(
        item,
        {"attempt_id", "radio_id", "plan_id", "requested_start_utc_ns"},
        "expected attempt",
    )
    return ExpectedCaptureAttempt(
        CaptureAttemptId(_string(item["attempt_id"], "attempt_id")),
        RadioId(_string(item["radio_id"], "radio_id")),
        PlanId(_string(item["plan_id"], "plan_id")),
        UtcNs(_integer(item["requested_start_utc_ns"], "requested_start_utc_ns")),
    )


def _outcome(value: object) -> CaptureAttemptOutcome:
    item = _object(value, "outcome")
    _fields(
        item,
        {
            "schema",
            "batch_id",
            "attempt_id",
            "radio_id",
            "plan_id",
            "state",
            "terminal_utc_ns",
            "observed_start_utc_ns",
            "recording_ref",
            "failure_reason",
        },
        "outcome",
    )
    recording = item["recording_ref"]
    return CaptureAttemptOutcome(
        _schema(item["schema"]),
        CaptureBatchId(_string(item["batch_id"], "batch_id")),
        CaptureAttemptId(_string(item["attempt_id"], "attempt_id")),
        RadioId(_string(item["radio_id"], "radio_id")),
        PlanId(_string(item["plan_id"], "plan_id")),
        CaptureAttemptState(_string(item["state"], "state")),
        UtcNs(_integer(item["terminal_utc_ns"], "terminal_utc_ns")),
        None
        if item["observed_start_utc_ns"] is None
        else UtcNs(_integer(item["observed_start_utc_ns"], "observed_start_utc_ns")),
        None if recording is None else _published(recording),
        _optional_string(item["failure_reason"], "failure_reason"),
    )


def _published(value: object) -> PublishedRecordingRef:
    item = _object(value, "published recording")
    _fields(item, {"recording_object"}, "published recording")
    recording = _object(item["recording_object"], "recording object")
    _fields(
        recording,
        {"recording_id", "data_object", "metadata_object", "manifest_digest"},
        "recording object",
    )
    return PublishedRecordingRef(
        RecordingObjectRef(
            RecordingId(_string(recording["recording_id"], "recording_id")),
            _object_ref(recording["data_object"]),
            _object_ref(recording["metadata_object"]),
            _digest(recording["manifest_digest"]),
        )
    )


def _object_ref(value: object) -> ObjectRef:
    item = _object(value, "object reference")
    _fields(
        item,
        {"digest", "byte_count", "media_type", "format_id", "locator"},
        "object reference",
    )
    return ObjectRef(
        _digest(item["digest"]),
        _integer(item["byte_count"], "byte_count"),
        _string(item["media_type"], "media_type"),
        _string(item["format_id"], "format_id"),
        _string(item["locator"], "locator"),
    )


def _digest(value: object) -> Digest:
    item = _object(value, "digest")
    _fields(item, {"algorithm", "value"}, "digest")
    return Digest(
        DigestAlgorithm(_string(item["algorithm"], "algorithm")),
        _string(item["value"], "digest value"),
    )


def _schema(value: object) -> SchemaRef:
    item = _object(value, "schema")
    _fields(item, {"schema_id", "version"}, "schema")
    version = _object(item["version"], "schema version")
    _fields(version, {"major", "minor"}, "schema version")
    return SchemaRef(
        _string(item["schema_id"], "schema_id"),
        SchemaVersion(
            _integer(version["major"], "schema major"),
            _integer(version["minor"], "schema minor"),
        ),
    )


def _document(encoded: bytes) -> dict[str, object]:
    try:
        value = json.loads(encoded, object_pairs_hook=_unique)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CaptureBatchDocumentError("batch document is not UTF-8 JSON") from error
    return _object(value, "batch document")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _bad(f"duplicate batch document key: {key}")
        result[key] = value
    return result


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _bad(f"{name} must be an object")
    return cast(dict[str, object], value)


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        _bad(f"{name} must be an array")
    return cast(list[object], value)


def _fields(value: dict[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        _bad(f"{name} fields differ")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        _bad(f"{name} must be a non-empty string")
    return value


def _optional_string(value: object, name: str) -> str | None:
    return None if value is None else _string(value, name)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _bad(f"{name} must be an integer")
    return value


def _optional_int(value: object, name: str) -> int | None:
    return None if value is None else _integer(value, name)


def _bad(message: str) -> NoReturn:
    raise CaptureBatchDocumentError(message)
