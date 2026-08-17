"""Exact durable completion evidence for one focused analysis pair."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ._validation import require_utc_ns
from .core import (
    CaptureBatchId,
    Digest,
    JobId,
    RecordingId,
    SchemaRef,
    UtcNs,
    canonical_json_bytes,
)


@dataclass(frozen=True, slots=True)
class FocusedAnalysisCompletionV0_1:
    schema: SchemaRef
    batch_id: CaptureBatchId
    capture_definition_digest: Digest
    recording_ids: tuple[RecordingId, RecordingId]
    recording_identity_digests: tuple[Digest, Digest]
    feature_job_ids: tuple[JobId, JobId]
    feature_result_digests: tuple[Digest, Digest]
    completed_utc_ns: UtcNs

    SCHEMA_ID = "org.leo-flow.focused-analysis-completion"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported focused completion schema")
        for values, label in (
            (self.recording_ids, "recording"),
            (self.recording_identity_digests, "recording identity"),
            (self.feature_job_ids, "feature job"),
            (self.feature_result_digests, "feature result"),
        ):
            if len(values) != 2 or len(set(values)) != 2:
                raise ValueError(f"focused completion requires two distinct {label}s")
        if tuple(sorted(self.recording_ids, key=str)) != self.recording_ids:
            raise ValueError("focused completion recordings must be canonical")
        require_utc_ns(self.completed_utc_ns, "completed_utc_ns")


def encode_focused_analysis_completion(value: FocusedAnalysisCompletionV0_1) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def decode_focused_analysis_completion(data: bytes) -> FocusedAnalysisCompletionV0_1:
    if len(data) > 16_384 or not data.endswith(b"\n"):
        raise ValueError("focused completion receipt is malformed")
    value = json.loads(data[:-1])
    if canonical_json_bytes(value) + b"\n" != data or not isinstance(value, dict):
        raise ValueError("focused completion receipt is not canonical")
    expected = set(FocusedAnalysisCompletionV0_1.__dataclass_fields__)
    if set(value) != expected:
        raise ValueError("focused completion receipt fields differ")
    return FocusedAnalysisCompletionV0_1(
        _schema(value["schema"]),
        CaptureBatchId(_string(value["batch_id"])),
        _digest(value["capture_definition_digest"]),
        tuple(RecordingId(_string(item)) for item in _array2(value["recording_ids"])),  # type: ignore[arg-type]
        tuple(_digest(item) for item in _array2(value["recording_identity_digests"])),  # type: ignore[arg-type]
        tuple(JobId(_string(item)) for item in _array2(value["feature_job_ids"])),  # type: ignore[arg-type]
        tuple(_digest(item) for item in _array2(value["feature_result_digests"])),  # type: ignore[arg-type]
        UtcNs(_integer(value["completed_utc_ns"])),
    )


def _schema(value: object) -> SchemaRef:
    item = _mapping(value)
    version = _mapping(item["version"])
    from .core import SchemaVersion

    return SchemaRef(
        _string(item["schema_id"]),
        SchemaVersion(_integer(version["major"]), _integer(version["minor"])),
    )


def _digest(value: object) -> Digest:
    from .core import DigestAlgorithm

    item = _mapping(value)
    return Digest(DigestAlgorithm(_string(item["algorithm"])), _string(item["value"]))


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("expected object")
    return value


def _array2(value: object) -> list[object]:
    if not isinstance(value, list) or len(value) != 2:
        raise TypeError("expected pair")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected string")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected integer")
    return value
