"""Canonical codec for the independent complete-IQ timeline product."""

from __future__ import annotations

import json

from leo_flow.contracts.core import (
    Digest,
    DigestAlgorithm,
    Provenance,
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
from leo_flow.contracts.starlink_full_dwell_timeline_product import (
    FullDwellTimelineBundleV0_1,
    FullDwellTimelinePlanV0_1,
    FullDwellTimelineStreamV0_1,
    FullDwellTimelineWindowV0_1,
)

MAX_FULL_DWELL_TIMELINE_BYTES = 64 * 1024 * 1024
FULL_DWELL_TIMELINE_MEDIA_TYPE = "application/json"
FULL_DWELL_TIMELINE_FORMAT_ID = "full-dwell-timeline-v0.1"


class MalformedFullDwellTimelineError(ValueError):
    pass


def encode_full_dwell_timeline(bundle: FullDwellTimelineBundleV0_1) -> bytes:
    payload = canonical_json_bytes(bundle)
    if len(payload) > MAX_FULL_DWELL_TIMELINE_BYTES:
        raise MalformedFullDwellTimelineError("timeline exceeds its byte bound")
    return payload


def decode_full_dwell_timeline(payload: bytes) -> FullDwellTimelineBundleV0_1:
    if len(payload) > MAX_FULL_DWELL_TIMELINE_BYTES:
        raise MalformedFullDwellTimelineError("timeline exceeds its byte bound")
    try:
        root = json.loads(payload, object_pairs_hook=_unique)
        if canonical_json_bytes(root) != payload:
            raise MalformedFullDwellTimelineError("timeline is not canonical JSON")
        item = _dict(root)
        result = FullDwellTimelineBundleV0_1(
            _schema(item["schema"]),
            _string(item["analysis_id"]),
            RecordingId(_string(item["recording_id"])),
            _digest(item["recording_identity_digest"]),
            _digest(item["request_digest"]),
            _plan(item["plan"]),
            tuple(_stream(value) for value in _list(item["streams"])),
            _provenance(item["provenance"]),
            tuple(_string(value) for value in _list(item["warnings"])),
            None,
        )
        if canonical_json_bytes(result) != payload:
            raise MalformedFullDwellTimelineError("timeline contains unknown fields")
        return result
    except MalformedFullDwellTimelineError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedFullDwellTimelineError(str(error)) from error


def _plan(value: object) -> FullDwellTimelinePlanV0_1:
    item = _dict(value)
    return FullDwellTimelinePlanV0_1(
        _integer(item["tile_sample_count"]),
        _integer(item["maximum_window_count_per_stream"]),
        _integer(item["maximum_refinements_per_stream"]),
        _string(item["metric"]),
        _string(item["tiling"]),
        _string(item["refinement_selection"]),
    )


def _stream(value: object) -> FullDwellTimelineStreamV0_1:
    item = _dict(value)
    return FullDwellTimelineStreamV0_1(
        RadioId(_string(item["radio_id"])),
        _string(item["lnb_id"]),
        SegmentId(_string(item["segment_id"])),
        ReceiverChainId(_string(item["receiver_chain_id"])),
        _integer(item["channel_number"]),
        StarlinkEdge(_string(item["edge"])),
        _number(item["sample_rate_hz"]),
        _integer(item["segment_sample_count"]),
        tuple(_window(entry) for entry in _list(item["windows"])),
        _integer(item["covered_sample_count"]),
        _number(item["coverage_fraction"]),
        _number(item["overlap_fraction"]),
        _boolean(item["refinement_is_data_adaptive"]),
    )


def _window(value: object) -> FullDwellTimelineWindowV0_1:
    item = _dict(value)
    rank = item["refinement_rank"]
    return FullDwellTimelineWindowV0_1(
        _integer(item["window_index"]),
        _integer(item["start_sample"]),
        _integer(item["stop_sample"]),
        UtcNs(_integer(item["interval_start_utc_ns"])),
        UtcNs(_integer(item["interval_stop_utc_ns"])),
        _number(item["mean_complex_power"]),
        None if rank is None else _integer(rank),
    )


def _provenance(value: object) -> Provenance:
    item = _dict(value)
    return Provenance(
        _string(item["producer_name"]),
        _string(item["producer_version"]),
        _string(item["git_commit"]),
        _digest(item["environment_digest"]),
        _digest(item["normalized_config_digest"]),
        tuple(_digest(entry) for entry in _list(item["input_digests"])),
        tuple(_digest(entry) for entry in _list(item["dependency_digests"])),
        UtcNs(_integer(item["started_utc_ns"])),
        UtcNs(_integer(item["completed_utc_ns"])),
        _string(item["host_class"]),
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
            raise MalformedFullDwellTimelineError("timeline contains duplicate keys")
        result[key] = value
    return result


def _dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise MalformedFullDwellTimelineError("expected object")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise MalformedFullDwellTimelineError("expected array")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise MalformedFullDwellTimelineError("expected string")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedFullDwellTimelineError("expected integer")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MalformedFullDwellTimelineError("expected number")
    return float(value)


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise MalformedFullDwellTimelineError("expected boolean")
    return value
