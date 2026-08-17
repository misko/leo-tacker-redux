"""Canonical codec for recording-level published-pilot constellation bundles."""

from __future__ import annotations

import json

from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    canonical_json_bytes,
)
from leo_flow.contracts.starlink_pilot_constellation_pipeline import (
    StarlinkPilotConstellationRecordingBundleV0_1,
)

from .starlink_pilot_constellation_codec import decode_starlink_pilot_constellation

MAX_STARLINK_PILOT_CONSTELLATION_RECORDING_BYTES = 64 * 1024 * 1024
STARLINK_PILOT_CONSTELLATION_RECORDING_MEDIA_TYPE = "application/json"
STARLINK_PILOT_CONSTELLATION_RECORDING_FORMAT_ID = (
    "starlink-pilot-constellation-recording-v0.1"
)


class MalformedStarlinkPilotConstellationRecordingError(ValueError):
    pass


def encode_starlink_pilot_constellation_recording(
    bundle: StarlinkPilotConstellationRecordingBundleV0_1,
) -> bytes:
    payload = canonical_json_bytes(bundle)
    if len(payload) > MAX_STARLINK_PILOT_CONSTELLATION_RECORDING_BYTES:
        raise MalformedStarlinkPilotConstellationRecordingError(
            "recording constellation exceeds size limit"
        )
    return payload


def decode_starlink_pilot_constellation_recording(
    data: bytes,
) -> StarlinkPilotConstellationRecordingBundleV0_1:
    if len(data) > MAX_STARLINK_PILOT_CONSTELLATION_RECORDING_BYTES:
        raise MalformedStarlinkPilotConstellationRecordingError(
            "recording constellation exceeds size limit"
        )
    try:
        root = json.loads(data, object_pairs_hook=_unique)
        if canonical_json_bytes(root) != data or not isinstance(root, dict):
            raise MalformedStarlinkPilotConstellationRecordingError(
                "recording constellation is not canonical"
            )
        expected = set(
            StarlinkPilotConstellationRecordingBundleV0_1.__dataclass_fields__
        )
        if set(root) != expected:
            raise MalformedStarlinkPilotConstellationRecordingError(
                "recording constellation has unexpected fields"
            )
        return StarlinkPilotConstellationRecordingBundleV0_1(
            _schema(root["schema"]),
            _string(root["analysis_id"]),
            RecordingId(_string(root["recording_id"])),
            _digest(root["recording_identity_digest"]),
            _artifact(root["source_suite_ref"]),
            _digest(root["source_suite_request_digest"]),
            _digest(root["request_digest"]),
            tuple(
                decode_starlink_pilot_constellation(canonical_json_bytes(item))
                for item in _array(root["streams"])
            ),
            tuple(_string(item) for item in _array(root["reason_codes"])),
            None
            if root["calibrated_detection_count"] is None
            else _integer(root["calibrated_detection_count"]),
        )
    except MalformedStarlinkPilotConstellationRecordingError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedStarlinkPilotConstellationRecordingError(str(error)) from error


def _artifact(value: object) -> ArtifactRef:
    item = _dict(value)
    return ArtifactRef(
        _string(item["artifact_id"]),
        _digest(item["digest"]),
        None if item["schema"] is None else _schema(item["schema"]),
    )


def _schema(value: object) -> SchemaRef:
    item = _dict(value)
    version = _dict(item["version"])
    return SchemaRef(
        _string(item["schema_id"]),
        SchemaVersion(_integer(version["major"]), _integer(version["minor"])),
    )


def _digest(value: object) -> Digest:
    item = _dict(value)
    return Digest(DigestAlgorithm(_string(item["algorithm"])), _string(item["value"]))


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MalformedStarlinkPilotConstellationRecordingError(
                f"duplicate JSON key: {key}"
            )
        result[key] = value
    return result


def _dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("expected object")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("expected array")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected string")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected integer")
    return value
