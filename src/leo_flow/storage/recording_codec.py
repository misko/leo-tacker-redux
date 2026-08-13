"""Dependency-free v1 CI16 data/metadata pair codec."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast

from leo_flow.contracts.capture import (
    ActivityKind,
    ActivityManifest,
    CapturePlan,
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
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.storage import RecordingObjectRef


class _BlobReader(Protocol):
    def head(self, ref: Any) -> Any: ...

    def open(self, ref: Any, byte_range: Any = None) -> Any: ...


class RecordingCodecError(RuntimeError):
    pass


class MalformedRecordingError(RecordingCodecError):
    pass


_META_SCHEMA = "org.leo-flow.recording-object-metadata"
_META_VERSION = "1.0"
_DATA_FILENAME = "recording.data"
_METADATA_FILENAME = "recording.meta"
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_SEGMENTS = 100_000
_INT64_MAX = (1 << 63) - 1


class SigMFRecordingWriter:
    """Selected v1 writer; physical names remain private to this adapter."""

    def begin(
        self,
        recording_id: RecordingId,
        plan: CapturePlan,
        hardware_metadata_snapshot_id: HardwareSnapshotId,
        destination: str,
    ) -> RecordingWriteSession:
        return RecordingWriteSession(
            recording_id, plan, hardware_metadata_snapshot_id, destination
        )


class RecordingWriteSession:
    def __init__(
        self,
        recording_id: RecordingId,
        plan: CapturePlan,
        hardware_metadata_snapshot_id: HardwareSnapshotId,
        destination: str,
    ) -> None:
        self._recording_id = recording_id
        self._plan = plan
        self._hardware_snapshot = hardware_metadata_snapshot_id
        self._final_dir = Path(destination)
        self._partial_dir = self._final_dir.with_name(self._final_dir.name + ".partial")
        self._partial_dir.mkdir(parents=True, exist_ok=False)
        self._data_path = self._partial_dir / _DATA_FILENAME
        self._data = self._data_path.open("xb", buffering=0)
        self._offsets: dict[SegmentId, tuple[int, int]] = {}
        self._finished: dict[SegmentId, SegmentManifest] = {}
        self._active_segment: SegmentId | None = None
        self._active_start = 0
        self._closed = False

    @property
    def recording_id(self) -> RecordingId:
        return self._recording_id

    def append_iq(self, segment_id: SegmentId, ci16_bytes: bytes) -> None:
        self._ensure_open()
        if segment_id in self._finished:
            raise RecordingCodecError("cannot append to a finished segment")
        if self._active_segment is None:
            self._active_segment = segment_id
            self._active_start = self._data.tell()
        elif self._active_segment != segment_id:
            raise RecordingCodecError("finish the current segment before another")
        if len(ci16_bytes) % 2:
            raise RecordingCodecError("CI16 byte blocks must align to int16 words")
        self._data.write(ci16_bytes)

    def finish_segment(self, segment: SegmentManifest) -> None:
        self._ensure_open()
        if self._active_segment != segment.segment_id:
            raise RecordingCodecError("segment was not active")
        stop = self._data.tell()
        expected = _checked_segment_bytes(segment.sample_count, segment.shape[1])
        if stop - self._active_start != expected:
            raise RecordingCodecError("segment byte count differs from manifest shape")
        self._offsets[segment.segment_id] = (self._active_start, expected)
        self._finished[segment.segment_id] = segment
        self._active_segment = None

    def finalize(self, manifest: RecordingManifest) -> CompletedLocalRecording:
        self._ensure_open()
        if manifest.recording_id != self.recording_id:
            raise RecordingCodecError("writer and manifest recording IDs differ")
        if manifest.plan_id != self._plan.plan_id:
            raise RecordingCodecError("manifest does not belong to writer plan")
        if manifest.hardware_metadata_snapshot_id != self._hardware_snapshot:
            raise RecordingCodecError("hardware snapshot changed during capture")
        if self._active_segment is not None:
            raise RecordingCodecError("active segment was not finished")
        if tuple(segment.segment_id for segment in manifest.segments) != tuple(
            self._finished
        ):
            raise RecordingCodecError("finished segment order differs from manifest")
        if any(
            self._finished[segment.segment_id] != segment
            for segment in manifest.segments
        ):
            raise RecordingCodecError("finished segment facts differ from manifest")

        self._data.flush()
        os.fsync(self._data.fileno())
        self._data.close()
        manifest_digest = canonical_digest(manifest)
        metadata = {
            "schema": _META_SCHEMA,
            "version": _META_VERSION,
            "core:datatype": "ci16_le",
            "leo:namespace_version": "1.0",
            "manifest": _wire(manifest),
            "manifest_digest": str(manifest_digest),
            "segments": [
                {
                    "segment_id": str(segment.segment_id),
                    "byte_offset": self._offsets[segment.segment_id][0],
                    "byte_count": self._offsets[segment.segment_id][1],
                    "shape": list(segment.shape),
                    "receiver_chain_ids": [
                        str(value) for value in segment.requested.receiver_chain_ids
                    ],
                }
                for segment in manifest.segments
            ],
        }
        metadata_bytes = canonical_json_bytes(metadata)
        metadata_path = self._partial_dir / _METADATA_FILENAME
        with metadata_path.open("xb", buffering=0) as output:
            output.write(metadata_bytes)
            output.flush()
            os.fsync(output.fileno())
        _fsync_directory(self._partial_dir)
        if self._final_dir.exists():
            raise RecordingCodecError("completed recording destination already exists")
        os.replace(self._partial_dir, self._final_dir)
        _fsync_directory(self._final_dir.parent)
        self._closed = True
        data_path = self._final_dir / _DATA_FILENAME
        metadata_path = self._final_dir / _METADATA_FILENAME
        return CompletedLocalRecording(
            manifest.recording_id,
            LocalObjectRef(
                str(data_path), _file_digest(data_path), data_path.stat().st_size
            ),
            LocalObjectRef(
                str(metadata_path),
                _file_digest(metadata_path),
                metadata_path.stat().st_size,
            ),
            manifest,
            manifest_digest,
        )

    def abort(self, reason: str) -> None:
        del reason
        if not self._data.closed:
            self._data.close()
        shutil.rmtree(self._partial_dir, ignore_errors=True)
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed or self._data.closed:
            raise RecordingCodecError("recording write session is closed")


class SigMFRecordingObjectReader:
    def __init__(self, blobs: _BlobReader) -> None:
        self._blobs = blobs

    @contextmanager
    def open(self, recording_ref: RecordingObjectRef) -> Iterator[RecordingView]:
        with self._blobs.open(recording_ref.metadata_object) as stream:
            metadata_bytes = stream.read(_MAX_METADATA_BYTES + 1)
        if len(metadata_bytes) > _MAX_METADATA_BYTES:
            raise MalformedRecordingError("recording metadata exceeds size limit")
        if Digest.sha256(metadata_bytes) != recording_ref.metadata_object.digest:
            raise MalformedRecordingError("metadata digest differs from reference")
        metadata = _parse_metadata(metadata_bytes, recording_ref)
        self._blobs.head(recording_ref.data_object)
        yield RecordingView(self._blobs, recording_ref, metadata)


class RecordingView:
    def __init__(
        self,
        blobs: _BlobReader,
        ref: RecordingObjectRef,
        metadata: _ParsedMetadata,
    ) -> None:
        self._blobs = blobs
        self._ref = ref
        self._metadata = metadata

    @property
    def manifest(self) -> RecordingManifest:
        return self._metadata.manifest

    def read_iq_bytes(
        self, segment_id: SegmentId, start_sample: int, stop_sample: int
    ) -> bytes:
        try:
            segment = self._metadata.segments[segment_id]
        except KeyError as error:
            raise KeyError(f"unknown segment {segment_id}") from error
        if not 0 <= start_sample < stop_sample <= segment.sample_count:
            raise ValueError("sample range lies outside segment")
        bytes_per_sample = segment.receiver_count * 2 * 2
        start = segment.byte_offset + start_sample * bytes_per_sample
        expected = (stop_sample - start_sample) * bytes_per_sample
        from leo_flow.contracts.storage import ByteRange

        with self._blobs.open(
            self._ref.data_object, ByteRange(start, start + expected)
        ) as stream:
            data = stream.read()
        if len(data) != expected:
            raise MalformedRecordingError("truncated data slice")
        return cast(bytes, data)


class _ParsedSegment:
    def __init__(
        self, byte_offset: int, byte_count: int, sample_count: int, receiver_count: int
    ) -> None:
        self.byte_offset = byte_offset
        self.byte_count = byte_count
        self.sample_count = sample_count
        self.receiver_count = receiver_count


class _ParsedMetadata:
    def __init__(
        self, manifest: RecordingManifest, segments: dict[SegmentId, _ParsedSegment]
    ) -> None:
        self.manifest = manifest
        self.segments = segments


def _parse_metadata(data: bytes, ref: RecordingObjectRef) -> _ParsedMetadata:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MalformedRecordingError("metadata is not valid UTF-8 JSON") from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != data:
        raise MalformedRecordingError("metadata is not canonical JSON")
    if set(value) != {
        "schema",
        "version",
        "core:datatype",
        "leo:namespace_version",
        "manifest",
        "manifest_digest",
        "segments",
    }:
        raise MalformedRecordingError("metadata has missing or unknown required fields")
    if value["schema"] != _META_SCHEMA or value["version"] != _META_VERSION:
        raise MalformedRecordingError("unsupported recording metadata schema")
    if value["core:datatype"] != "ci16_le" or value["leo:namespace_version"] != "1.0":
        raise MalformedRecordingError("unsupported data representation")
    try:
        manifest = _manifest_from_wire(_mapping(value["manifest"], "manifest"))
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedRecordingError("invalid embedded manifest") from error
    if manifest.recording_id != ref.recording_id:
        raise MalformedRecordingError("metadata recording ID differs")
    if canonical_digest(manifest) != ref.manifest_digest:
        raise MalformedRecordingError("embedded manifest digest differs")
    if value["manifest_digest"] != str(ref.manifest_digest):
        raise MalformedRecordingError("declared manifest digest differs")
    raw_segments = value["segments"]
    if not isinstance(raw_segments, list) or not 0 < len(raw_segments) <= _MAX_SEGMENTS:
        raise MalformedRecordingError("invalid segment table size")
    if len(raw_segments) != len(manifest.segments):
        raise MalformedRecordingError("segment table and manifest differ")
    parsed: dict[SegmentId, _ParsedSegment] = {}
    prior_stop = 0
    for raw, manifest_segment in zip(raw_segments, manifest.segments, strict=True):
        item = _mapping(raw, "segment")
        if set(item) != {
            "segment_id",
            "byte_offset",
            "byte_count",
            "shape",
            "receiver_chain_ids",
        }:
            raise MalformedRecordingError("segment metadata fields differ")
        segment_id = SegmentId(_string(item["segment_id"], "segment_id"))
        if segment_id != manifest_segment.segment_id or segment_id in parsed:
            raise MalformedRecordingError("duplicate or reordered segment ID")
        offset = _nonnegative_int(item["byte_offset"], "byte_offset")
        count = _positive_int(item["byte_count"], "byte_count")
        shape = _int_list(item["shape"], "shape", length=3)
        receiver_ids = _string_list(item["receiver_chain_ids"], "receiver_chain_ids")
        if tuple(shape) != manifest_segment.shape:
            raise MalformedRecordingError("segment shape differs from manifest")
        if tuple(receiver_ids) != tuple(
            map(str, manifest_segment.requested.receiver_chain_ids)
        ):
            raise MalformedRecordingError("receiver order differs from manifest")
        expected = _checked_segment_bytes(shape[0], shape[1])
        if count != expected or offset != prior_stop:
            raise MalformedRecordingError(
                "segment ranges overlap, gap, or have wrong size"
            )
        stop = _checked_add(offset, count)
        if stop > ref.data_object.byte_count:
            raise MalformedRecordingError("segment exceeds data object")
        parsed[segment_id] = _ParsedSegment(offset, count, shape[0], shape[1])
        prior_stop = stop
    if prior_stop != ref.data_object.byte_count:
        raise MalformedRecordingError("data object has truncation or trailing bytes")
    return _ParsedMetadata(manifest, parsed)


def _wire(value: Any) -> Any:
    if is_dataclass(value):
        return {
            field.name: _wire(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_wire(item) for item in value]
    return value


def _manifest_from_wire(value: Mapping[str, Any]) -> RecordingManifest:
    schema = _schema(value["schema"])
    activities = tuple(
        _activity_manifest(item) for item in _list(value["activities"], "activities")
    )
    segments = tuple(
        _segment_manifest(item) for item in _list(value["segments"], "segments")
    )
    return RecordingManifest(
        schema,
        RecordingId(_string(value["recording_id"], "recording_id")),
        UtcNs(_nonnegative_int(value["created_utc_ns"], "created_utc_ns")),
        UtcNs(
            _nonnegative_int(value["capture_started_utc_ns"], "capture_started_utc_ns")
        ),
        UtcNs(
            _nonnegative_int(
                value["capture_finished_utc_ns"], "capture_finished_utc_ns"
            )
        ),
        StationId(_string(value["station_id"], "station_id")),
        RadioId(_string(value["radio_id"], "radio_id")),
        _string(value["radio_serial"], "radio_serial"),
        tuple(
            ReceiverChainId(item)
            for item in _string_list(value["receiver_chain_ids"], "receiver_chain_ids")
        ),
        _string(value["clock_status"], "clock_status"),
        HardwareSnapshotId(
            _string(
                value["hardware_metadata_snapshot_id"], "hardware_metadata_snapshot_id"
            )
        ),
        activities,
        segments,
        PlanId(_string(value["plan_id"], "plan_id")),
        _string(value["producer"], "producer"),
        _frozen_mapping(value["experiment_tags"], "experiment_tags"),
        _string(value["sample_dtype"], "sample_dtype"),
        _three_strings(value["sample_layout"], "sample_layout"),
        _string(value["state"], "state"),
    )


def _segment_manifest(value: Any) -> SegmentManifest:
    item = _mapping(value, "segment manifest")
    requested = _segment_request(item["requested"])
    gain = _gain(item["actual_gain"])
    return SegmentManifest(
        SegmentId(_string(item["segment_id"], "segment_id")),
        requested,
        _number(item["actual_center_frequency_hz"], "actual_center_frequency_hz"),
        _number(item["actual_sample_rate_hz"], "actual_sample_rate_hz"),
        _number(item["actual_bandwidth_hz"], "actual_bandwidth_hz"),
        gain,
        UtcNs(_nonnegative_int(item["start_utc_ns"], "start_utc_ns")),
        _nonnegative_int(item["monotonic_start_ns"], "monotonic_start_ns"),
        _positive_int(item["sample_count"], "sample_count"),
        _three_ints(item["shape"], "shape"),
        _frozen_mapping(item["diagnostics"], "diagnostics"),
    )


def _segment_request(value: Any) -> SegmentRequest:
    item = _mapping(value, "segment request")
    return SegmentRequest(
        SegmentId(_string(item["segment_id"], "segment_id")),
        _number(item["center_frequency_hz"], "center_frequency_hz"),
        _number(item["sample_rate_hz"], "sample_rate_hz"),
        _number(item["bandwidth_hz"], "bandwidth_hz"),
        tuple(
            ReceiverChainId(v)
            for v in _string_list(item["receiver_chain_ids"], "receiver_chain_ids")
        ),
        _gain(item["gain"]),
        _optional_number(item["duration_s"], "duration_s"),
        _optional_int(item["sample_count"], "sample_count"),
        UtcNs(item["scheduled_utc_ns"])
        if item["scheduled_utc_ns"] is not None
        else None,
        _frozen_mapping(item["hardware_controls"], "hardware_controls"),
        _frozen_mapping(item["tags"], "tags"),
    )


def _activity_manifest(value: Any) -> ActivityManifest:
    item = _mapping(value, "activity manifest")
    return ActivityManifest(
        ActivityId(_string(item["activity_id"], "activity_id")),
        ActivityKind(_string(item["kind"], "kind")),
        UtcNs(_nonnegative_int(item["started_utc_ns"], "started_utc_ns")),
        UtcNs(_nonnegative_int(item["finished_utc_ns"], "finished_utc_ns")),
        tuple(SegmentId(v) for v in _string_list(item["segment_ids"], "segment_ids")),
    )


def _gain(value: Any) -> GainSetting:
    item = _mapping(value, "gain")
    gain = item["gain_db"]
    return GainSetting(
        GainMode(_string(item["mode"], "gain mode")),
        None if gain is None else _number(gain, "gain_db"),
    )


def _schema(value: Any) -> SchemaRef:
    item = _mapping(value, "schema")
    version = _mapping(item["version"], "schema version")
    return SchemaRef(
        _string(item["schema_id"], "schema_id"),
        SchemaVersion(
            _nonnegative_int(version["major"], "major"),
            _nonnegative_int(version["minor"], "minor"),
        ),
    )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be an array")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be numeric")
    return float(value)


def _nonnegative_int(value: Any, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _INT64_MAX
    ):
        raise ValueError(f"{field} must be a bounded non-negative integer")
    return value


def _positive_int(value: Any, field: str) -> int:
    result = _nonnegative_int(value, field)
    if result == 0:
        raise ValueError(f"{field} must be positive")
    return result


def _optional_int(value: Any, field: str) -> int | None:
    return None if value is None else _positive_int(value, field)


def _optional_number(value: Any, field: str) -> float | None:
    return None if value is None else _number(value, field)


def _int_list(value: Any, field: str, *, length: int) -> list[int]:
    result = [_nonnegative_int(item, field) for item in _list(value, field)]
    if len(result) != length:
        raise ValueError(f"{field} must contain {length} integers")
    return result


def _string_list(value: Any, field: str) -> list[str]:
    return [_string(item, field) for item in _list(value, field)]


def _three_ints(value: Any, field: str) -> tuple[int, int, int]:
    items = _int_list(value, field, length=3)
    return items[0], items[1], items[2]


def _three_strings(value: Any, field: str) -> tuple[str, str, str]:
    items = _string_list(value, field)
    if len(items) != 3:
        raise ValueError(f"{field} must contain three strings")
    return items[0], items[1], items[2]


def _frozen_mapping(value: Any, field: str) -> tuple[tuple[str, Any], ...]:
    items = _list(value, field)
    result: list[tuple[str, Any]] = []
    for pair in items:
        if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
            raise TypeError(f"{field} must contain [key, value] pairs")
        result.append((pair[0], _freeze_json(pair[1])))
    if result != sorted(result) or len({key for key, _ in result}) != len(result):
        raise ValueError(f"{field} keys must be unique and canonical")
    return tuple(result)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple((key, _freeze_json(item)) for key, item in sorted(value.items()))
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _checked_segment_bytes(sample_count: int, receiver_count: int) -> int:
    if sample_count <= 0 or receiver_count <= 0:
        raise MalformedRecordingError("sample and receiver counts must be positive")
    if sample_count > _INT64_MAX // receiver_count // 4:
        raise MalformedRecordingError("segment byte size overflows")
    return sample_count * receiver_count * 4


def _checked_add(left: int, right: int) -> int:
    if left > _INT64_MAX - right:
        raise MalformedRecordingError("segment range overflows")
    return left + right


def _file_digest(path: Path) -> Digest:
    hasher = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return Digest(DigestAlgorithm.SHA256, hasher.hexdigest())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
