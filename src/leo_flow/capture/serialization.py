"""Private SQLite-spool encoding for frozen capture contracts."""

from __future__ import annotations

import json
from typing import Any

from leo_flow.contracts.capture import (
    ActivityKind,
    ActivityManifest,
    CompletedLocalRecording,
    GainMode,
    GainSetting,
    LocalObjectRef,
    RecordingManifest,
    SegmentManifest,
    SegmentRequest,
)
from leo_flow.contracts.core import (
    ActivityId,
    Digest,
    DigestAlgorithm,
    HardwareSnapshotId,
    PlanId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    SegmentId,
    StationId,
    UtcNs,
    canonical_json_bytes,
)


def encode_completed(recording: CompletedLocalRecording) -> bytes:
    return canonical_json_bytes(recording)


def decode_completed(encoded: bytes) -> CompletedLocalRecording:
    value = json.loads(encoded)
    manifest = _manifest(value["manifest"])
    return CompletedLocalRecording(
        recording_id=RecordingId(value["recording_id"]),
        data_object=_local_object(value["data_object"]),
        metadata_object=_local_object(value["metadata_object"]),
        manifest=manifest,
        manifest_digest=_digest(value["manifest_digest"]),
    )


def _digest(value: dict[str, Any]) -> Digest:
    return Digest(DigestAlgorithm(value["algorithm"]), value["value"])


def _schema(value: dict[str, Any]) -> SchemaRef:
    version = value["version"]
    return SchemaRef(
        value["schema_id"], SchemaVersion(version["major"], version["minor"])
    )


def _gain(value: dict[str, Any]) -> GainSetting:
    return GainSetting(GainMode(value["mode"]), value["gain_db"])


def _immutable_json(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_immutable_json(item) for item in value)
    if isinstance(value, dict):
        return tuple(
            (key, _immutable_json(item)) for key, item in sorted(value.items())
        )
    return value


def _pairs(value: list[list[Any]]) -> tuple[tuple[str, Any], ...]:
    return tuple((str(key), _immutable_json(item)) for key, item in value)


def _segment_request(value: dict[str, Any]) -> SegmentRequest:
    scheduled = value["scheduled_utc_ns"]
    return SegmentRequest(
        segment_id=SegmentId(value["segment_id"]),
        center_frequency_hz=value["center_frequency_hz"],
        sample_rate_hz=value["sample_rate_hz"],
        bandwidth_hz=value["bandwidth_hz"],
        receiver_chain_ids=tuple(
            ReceiverChainId(item) for item in value["receiver_chain_ids"]
        ),
        gain=_gain(value["gain"]),
        duration_s=value["duration_s"],
        sample_count=value["sample_count"],
        scheduled_utc_ns=UtcNs(scheduled) if scheduled is not None else None,
        hardware_controls=_pairs(value["hardware_controls"]),
        tags=_pairs(value["tags"]),
    )


def _segment_manifest(value: dict[str, Any]) -> SegmentManifest:
    return SegmentManifest(
        segment_id=SegmentId(value["segment_id"]),
        requested=_segment_request(value["requested"]),
        actual_center_frequency_hz=value["actual_center_frequency_hz"],
        actual_sample_rate_hz=value["actual_sample_rate_hz"],
        actual_bandwidth_hz=value["actual_bandwidth_hz"],
        actual_gain=_gain(value["actual_gain"]),
        start_utc_ns=UtcNs(value["start_utc_ns"]),
        monotonic_start_ns=value["monotonic_start_ns"],
        sample_count=value["sample_count"],
        shape=tuple(value["shape"]),
        diagnostics=_pairs(value["diagnostics"]),
    )


def _activity_manifest(value: dict[str, Any]) -> ActivityManifest:
    return ActivityManifest(
        activity_id=ActivityId(value["activity_id"]),
        kind=ActivityKind(value["kind"]),
        started_utc_ns=UtcNs(value["started_utc_ns"]),
        finished_utc_ns=UtcNs(value["finished_utc_ns"]),
        segment_ids=tuple(SegmentId(item) for item in value["segment_ids"]),
    )


def _manifest(value: dict[str, Any]) -> RecordingManifest:
    return RecordingManifest(
        schema=_schema(value["schema"]),
        recording_id=RecordingId(value["recording_id"]),
        created_utc_ns=UtcNs(value["created_utc_ns"]),
        capture_started_utc_ns=UtcNs(value["capture_started_utc_ns"]),
        capture_finished_utc_ns=UtcNs(value["capture_finished_utc_ns"]),
        station_id=StationId(value["station_id"]),
        radio_id=RadioId(value["radio_id"]),
        radio_serial=value["radio_serial"],
        receiver_chain_ids=tuple(
            ReceiverChainId(item) for item in value["receiver_chain_ids"]
        ),
        clock_status=value["clock_status"],
        hardware_metadata_snapshot_id=HardwareSnapshotId(
            value["hardware_metadata_snapshot_id"]
        ),
        activities=tuple(_activity_manifest(item) for item in value["activities"]),
        segments=tuple(_segment_manifest(item) for item in value["segments"]),
        plan_id=PlanId(value["plan_id"]),
        producer=value["producer"],
        experiment_tags=_pairs(value["experiment_tags"]),
        sample_dtype=value["sample_dtype"],
        sample_layout=tuple(value["sample_layout"]),
        state=value["state"],
    )


def _local_object(value: dict[str, Any]) -> LocalObjectRef:
    return LocalObjectRef(
        locator=value["locator"],
        digest=_digest(value["digest"]),
        byte_count=value["byte_count"],
    )
