"""Canonical bounded codec for adaptive QAM v0.4 products."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import NoReturn

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
    canonical_json_bytes,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_adaptive_qam import (
    AdaptiveQamSelectionReason,
    StarlinkAdaptiveQamBundleV0_4,
    StarlinkAdaptiveQamRequestV0_4,
    StarlinkAdaptiveQamStreamRequestV0_4,
    StarlinkAdaptiveQamWindowSelectionV0_4,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef

from .starlink_acquired_constellation_recording_codec import (
    decode_starlink_acquired_constellation_recording,
)

MAX_STARLINK_ADAPTIVE_QAM_BYTES = 256 * 1024 * 1024
STARLINK_ADAPTIVE_QAM_MEDIA_TYPE = "application/vnd.leo-flow.starlink-adaptive-qam+json"
STARLINK_ADAPTIVE_QAM_FORMAT_ID = "starlink-adaptive-qam-json-v0.4"


class MalformedStarlinkAdaptiveQamError(ValueError):
    pass


def encode_starlink_adaptive_qam(bundle: StarlinkAdaptiveQamBundleV0_4) -> bytes:
    payload = canonical_json_bytes(bundle)
    if len(payload) > MAX_STARLINK_ADAPTIVE_QAM_BYTES:
        raise ValueError("adaptive QAM product exceeds its byte bound")
    return payload


def decode_starlink_adaptive_qam(payload: bytes) -> StarlinkAdaptiveQamBundleV0_4:
    if not payload or len(payload) > MAX_STARLINK_ADAPTIVE_QAM_BYTES:
        raise MalformedStarlinkAdaptiveQamError("adaptive QAM bytes are invalid")
    try:
        root = json.loads(payload, object_pairs_hook=_unique_object)
        item = _mapping(root)
        _keys(item, StarlinkAdaptiveQamBundleV0_4.__dataclass_fields__)
        evidence = decode_starlink_acquired_constellation_recording(
            canonical_json_bytes(item["evidence_bundle"])
        )
        bundle = StarlinkAdaptiveQamBundleV0_4(
            _schema(_mapping(item["schema"])),
            _string(item["analysis_id"]),
            RecordingId(_string(item["recording_id"])),
            _digest(_mapping(item["recording_identity_digest"])),
            _artifact(_mapping(item["source_adaptive_response_ref"])),
            _artifact(_mapping(item["source_suite_ref"])),
            _digest(_mapping(item["request_digest"])),
            tuple(
                _stream(_mapping(value)) for value in _array(item["stream_selections"])
            ),
            evidence,
            tuple(_string(value) for value in _array(item["warnings"])),
            None if item["calibrated_detection_count"] is None else _invalid_none(),
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as error:
        raise MalformedStarlinkAdaptiveQamError(
            "adaptive QAM product is malformed"
        ) from error
    if encode_starlink_adaptive_qam(bundle) != payload:
        raise MalformedStarlinkAdaptiveQamError("adaptive QAM bytes are noncanonical")
    return bundle


def _stream(item: dict[str, object]) -> StarlinkAdaptiveQamStreamRequestV0_4:
    _keys(item, StarlinkAdaptiveQamStreamRequestV0_4.__dataclass_fields__)
    return StarlinkAdaptiveQamStreamRequestV0_4(
        RadioId(_string(item["radio_id"])),
        _string(item["lnb_id"]),
        SegmentId(_string(item["segment_id"])),
        ReceiverChainId(_string(item["receiver_chain_id"])),
        _integer(item["channel_number"]),
        StarlinkEdge(_string(item["edge"])),
        _number(item["sample_rate_hz"]),
        _integer(item["segment_sample_count"]),
        tuple(_selection(_mapping(value)) for value in _array(item["windows"])),
    )


def _selection(item: dict[str, object]) -> StarlinkAdaptiveQamWindowSelectionV0_4:
    _keys(item, StarlinkAdaptiveQamWindowSelectionV0_4.__dataclass_fields__)
    return StarlinkAdaptiveQamWindowSelectionV0_4(
        _integer(item["source_window_index"]),
        _integer(item["source_start_sample"]),
        _integer(item["source_stop_sample"]),
        _integer(item["qam_start_sample"]),
        _integer(item["qam_stop_sample"]),
        tuple(
            AdaptiveQamSelectionReason(_string(value))
            for value in _array(item["reasons"])
        ),
        _number(item["source_qin_score"]),
        _number(item["source_max_surrogate_score"]),
        _number(item["source_qin_minus_max_surrogate"]),
    )


def _request(item: dict[str, object]) -> StarlinkAdaptiveQamRequestV0_4:
    _keys(item, StarlinkAdaptiveQamRequestV0_4.__dataclass_fields__)
    return StarlinkAdaptiveQamRequestV0_4(
        _schema(_mapping(item["schema"])),
        RecordingId(_string(item["recording_id"])),
        _recording_ref(_mapping(item["recording_object_ref"])),
        _artifact(_mapping(item["source_adaptive_response_ref"])),
        _artifact(_mapping(item["source_suite_ref"])),
        tuple(_stream(_mapping(value)) for value in _array(item["streams"])),
        _schema(_mapping(item["requested_output_schema"])),
    )


def _recording_ref(item: dict[str, object]) -> RecordingObjectRef:
    return RecordingObjectRef(
        RecordingId(_string(item["recording_id"])),
        _object(_mapping(item["data_object"])),
        _object(_mapping(item["metadata_object"])),
        _digest(_mapping(item["manifest_digest"])),
    )


def _object(item: dict[str, object]) -> ObjectRef:
    return ObjectRef(
        _digest(_mapping(item["digest"])),
        _integer(item["byte_count"]),
        _string(item["media_type"]),
        _string(item["format_id"]),
        _string(item["locator"]),
    )


def _artifact(item: dict[str, object]) -> ArtifactRef:
    schema = item["schema"]
    return ArtifactRef(
        _string(item["artifact_id"]),
        _digest(_mapping(item["digest"])),
        None if schema is None else _schema(_mapping(schema)),
    )


def _schema(item: dict[str, object]) -> SchemaRef:
    version = _mapping(item["version"])
    return SchemaRef(
        _string(item["schema_id"]),
        SchemaVersion(_integer(version["major"]), _integer(version["minor"])),
    )


def _digest(item: dict[str, object]) -> Digest:
    return Digest(DigestAlgorithm(_string(item["algorithm"])), _string(item["value"]))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _keys(item: dict[str, object], expected: Mapping[str, object]) -> None:
    names = set(expected)
    if set(item) != names:
        raise ValueError("JSON object keys differ")


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("expected JSON object")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("expected JSON array")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected string")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected integer")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("expected number")
    return float(value)


def _invalid_none() -> NoReturn:
    raise ValueError("calibrated detection count must be null")
