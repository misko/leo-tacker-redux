"""Canonical codec for recording-level paired Starlink surrogate evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping

from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    SegmentId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_surrogate_null_pipeline import (
    StarlinkSurrogateNullRecordingBundleV0_1,
    StarlinkSurrogateNullRecordingState,
    StarlinkSurrogateNullStreamEvidenceV0_1,
)

from .starlink_surrogate_null_codec import decode_paired_surrogate_evidence

MAX_STARLINK_SURROGATE_NULL_RECORDING_BYTES = 128 * 1024 * 1024
STARLINK_SURROGATE_NULL_RECORDING_MEDIA_TYPE = "application/json"
STARLINK_SURROGATE_NULL_RECORDING_FORMAT_ID = (
    "starlink-surrogate-null-recording-bundle-v0.1"
)


class MalformedStarlinkSurrogateNullRecordingError(ValueError):
    pass


def encode_starlink_surrogate_null_recording(
    bundle: StarlinkSurrogateNullRecordingBundleV0_1,
) -> bytes:
    payload = canonical_json_bytes(bundle)
    if len(payload) > MAX_STARLINK_SURROGATE_NULL_RECORDING_BYTES:
        raise MalformedStarlinkSurrogateNullRecordingError(
            "surrogate-null recording exceeds size limit"
        )
    return payload


def decode_starlink_surrogate_null_recording(
    data: bytes,
) -> StarlinkSurrogateNullRecordingBundleV0_1:
    if len(data) > MAX_STARLINK_SURROGATE_NULL_RECORDING_BYTES:
        raise MalformedStarlinkSurrogateNullRecordingError(
            "surrogate-null recording exceeds size limit"
        )
    try:
        value = json.loads(data, object_pairs_hook=_unique)
        if canonical_json_bytes(value) != data:
            raise MalformedStarlinkSurrogateNullRecordingError(
                "surrogate-null recording bytes are not canonical"
            )
        root = _mapping(value)
        if root["calibrated_detection_count"] is not None:
            raise MalformedStarlinkSurrogateNullRecordingError(
                "surrogate-null recording cannot contain a detection count"
            )
        result = StarlinkSurrogateNullRecordingBundleV0_1(
            _schema(root["schema"]),
            _string(root["analysis_id"]),
            RecordingId(_string(root["recording_id"])),
            _digest(root["recording_identity_digest"]),
            _artifact(root["source_suite_ref"]),
            _digest(root["source_suite_request_digest"]),
            _digest(root["request_digest"]),
            StarlinkSurrogateNullRecordingState(_string(root["state"])),
            tuple(_stream(item) for item in _array(root["streams"])),
            tuple(_string(item) for item in _array(root["reason_codes"])),
            None,
        )
        if canonical_json_bytes(result) != data:
            raise MalformedStarlinkSurrogateNullRecordingError(
                "surrogate-null recording has unknown or noncanonical fields"
            )
        return result
    except MalformedStarlinkSurrogateNullRecordingError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedStarlinkSurrogateNullRecordingError(str(error)) from error


def _stream(value: object) -> StarlinkSurrogateNullStreamEvidenceV0_1:
    item = _mapping(value)
    evidence_bytes = canonical_json_bytes(_mapping(item["evidence"]))
    return StarlinkSurrogateNullStreamEvidenceV0_1(
        RadioId(_string(item["radio_id"])),
        SegmentId(_string(item["segment_id"])),
        ReceiverChainId(_string(item["receiver_chain_id"])),
        _integer(item["channel_number"]),
        StarlinkEdge(_string(item["edge"])),
        UtcNs(_integer(item["interval_start_utc_ns"])),
        UtcNs(_integer(item["interval_stop_utc_ns"])),
        decode_paired_surrogate_evidence(evidence_bytes),
    )


def _artifact(value: object) -> ArtifactRef:
    item = _mapping(value)
    return ArtifactRef(
        _string(item["artifact_id"]),
        _digest(item["digest"]),
        None if item["schema"] is None else _schema(item["schema"]),
    )


def _schema(value: object) -> SchemaRef:
    item = _mapping(value)
    version = _mapping(item["version"])
    return SchemaRef(
        _string(item["schema_id"]),
        SchemaVersion(_integer(version["major"]), _integer(version["minor"])),
    )


def _digest(value: object) -> Digest:
    item = _mapping(value)
    return Digest(DigestAlgorithm(_string(item["algorithm"])), _string(item["value"]))


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MalformedStarlinkSurrogateNullRecordingError("expected object")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise MalformedStarlinkSurrogateNullRecordingError("expected array")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise MalformedStarlinkSurrogateNullRecordingError("expected string")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedStarlinkSurrogateNullRecordingError("expected integer")
    return value


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MalformedStarlinkSurrogateNullRecordingError("duplicate object key")
        result[key] = value
    return result
