"""Declarative capture plan and factual recording manifest contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ._validation import freeze_mapping, require_positive, require_utc_ns
from .core import (
    V0_1,
    ActivityId,
    Digest,
    HardwareSnapshotId,
    PlanId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    StationId,
    UtcNs,
)


class ActivityKind(str, Enum):
    SCAN = "scan"
    DWELL = "dwell"
    CALIBRATION = "calibration"
    TEST = "test"


class GainMode(str, Enum):
    MANUAL = "manual"
    AGC = "agc"


@dataclass(frozen=True)
class GainSetting:
    mode: GainMode
    gain_db: float | None = None

    def __post_init__(self) -> None:
        if self.mode is GainMode.MANUAL and self.gain_db is None:
            raise ValueError("manual gain requires gain_db")
        if self.mode is GainMode.AGC and self.gain_db is not None:
            raise ValueError("AGC gain cannot declare gain_db")


@dataclass(frozen=True)
class SegmentRequest:
    segment_id: SegmentId
    center_frequency_hz: float
    sample_rate_hz: float
    bandwidth_hz: float
    receiver_chain_ids: tuple[ReceiverChainId, ...]
    gain: GainSetting
    duration_s: float | None = None
    sample_count: int | None = None
    scheduled_utc_ns: UtcNs | None = None
    hardware_controls: tuple[tuple[str, Any], ...] = ()
    tags: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        for name in ("center_frequency_hz", "sample_rate_hz", "bandwidth_hz"):
            require_positive(getattr(self, name), name)
        if (self.duration_s is None) == (self.sample_count is None):
            raise ValueError("exactly one of duration_s and sample_count is required")
        if self.duration_s is not None:
            require_positive(self.duration_s, "duration_s")
        if self.sample_count is not None:
            require_positive(self.sample_count, "sample_count")
        if not self.receiver_chain_ids or len(set(self.receiver_chain_ids)) != len(
            self.receiver_chain_ids
        ):
            raise ValueError("receiver_chain_ids must be non-empty and unique")
        if self.scheduled_utc_ns is not None:
            require_utc_ns(self.scheduled_utc_ns, "scheduled_utc_ns")

    @classmethod
    def create(
        cls,
        *,
        hardware_controls: dict[str, Any] | None = None,
        tags: dict[str, Any] | None = None,
        **values: Any,
    ) -> SegmentRequest:
        return cls(
            hardware_controls=freeze_mapping(
                hardware_controls or {}, "hardware_controls"
            ),
            tags=freeze_mapping(tags or {}, "tags"),
            **values,
        )


@dataclass(frozen=True)
class ActivityRequest:
    activity_id: ActivityId
    kind: ActivityKind
    segments: tuple[SegmentRequest, ...]

    def __post_init__(self) -> None:
        if not self.segments:
            raise ValueError("activity requires at least one segment")
        if len({segment.segment_id for segment in self.segments}) != len(self.segments):
            raise ValueError("segment IDs must be unique within an activity")


@dataclass(frozen=True)
class CapturePlan:
    schema: SchemaRef
    plan_id: PlanId
    radio_id: RadioId
    receiver_chain_ids: tuple[ReceiverChainId, ...]
    activities: tuple[ActivityRequest, ...]
    experiment_tags: tuple[tuple[str, Any], ...] = ()

    SCHEMA_ID = "org.leo-flow.capture-plan"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported capture plan schema")
        if not self.activities:
            raise ValueError("capture plan requires an activity")
        if len({activity.activity_id for activity in self.activities}) != len(
            self.activities
        ):
            raise ValueError("activity IDs must be unique")
        allowed = set(self.receiver_chain_ids)
        if not allowed or any(
            not set(segment.receiver_chain_ids) <= allowed
            for activity in self.activities
            for segment in activity.segments
        ):
            raise ValueError("segments may only use plan receiver chains")


@dataclass(frozen=True)
class ActivityManifest:
    activity_id: ActivityId
    kind: ActivityKind
    started_utc_ns: UtcNs
    finished_utc_ns: UtcNs
    segment_ids: tuple[SegmentId, ...]

    def __post_init__(self) -> None:
        require_utc_ns(self.started_utc_ns, "started_utc_ns")
        require_utc_ns(self.finished_utc_ns, "finished_utc_ns")
        if self.finished_utc_ns <= self.started_utc_ns or not self.segment_ids:
            raise ValueError("activity interval and segments must be non-empty")


@dataclass(frozen=True)
class SegmentManifest:
    segment_id: SegmentId
    requested: SegmentRequest
    actual_center_frequency_hz: float
    actual_sample_rate_hz: float
    actual_bandwidth_hz: float
    actual_gain: GainSetting
    start_utc_ns: UtcNs
    monotonic_start_ns: int
    sample_count: int
    shape: tuple[int, int, int]
    diagnostics: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.segment_id != self.requested.segment_id:
            raise ValueError("requested and actual segment IDs differ")
        require_utc_ns(self.start_utc_ns, "start_utc_ns")
        for name in (
            "actual_center_frequency_hz",
            "actual_sample_rate_hz",
            "actual_bandwidth_hz",
        ):
            require_positive(getattr(self, name), name)
        require_positive(self.sample_count, "sample_count")
        if self.monotonic_start_ns < 0:
            raise ValueError("monotonic_start_ns must be non-negative")
        expected = (self.sample_count, len(self.requested.receiver_chain_ids), 2)
        if self.shape != expected:
            raise ValueError(f"shape must be {expected}")


@dataclass(frozen=True)
class RecordingManifest:
    schema: SchemaRef
    recording_id: RecordingId
    created_utc_ns: UtcNs
    capture_started_utc_ns: UtcNs
    capture_finished_utc_ns: UtcNs
    station_id: StationId
    radio_id: RadioId
    radio_serial: str
    receiver_chain_ids: tuple[ReceiverChainId, ...]
    clock_status: str
    hardware_metadata_snapshot_id: HardwareSnapshotId
    activities: tuple[ActivityManifest, ...]
    segments: tuple[SegmentManifest, ...]
    plan_id: PlanId
    producer: str
    experiment_tags: tuple[tuple[str, Any], ...] = ()
    sample_dtype: str = "<i2"
    sample_layout: tuple[str, str, str] = ("sample", "receiver", "component")
    state: str = "complete"

    SCHEMA_ID = "org.leo-flow.recording-manifest"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported recording manifest schema")
        for field_name in (
            "created_utc_ns",
            "capture_started_utc_ns",
            "capture_finished_utc_ns",
        ):
            require_utc_ns(getattr(self, field_name), field_name)
        if (
            not self.created_utc_ns
            <= self.capture_started_utc_ns
            < self.capture_finished_utc_ns
        ):
            raise ValueError("recording timestamps are inconsistent")
        if self.state != "complete":
            raise ValueError("published recording manifests must be complete")
        if self.sample_dtype != "<i2" or self.sample_layout != (
            "sample",
            "receiver",
            "component",
        ):
            raise ValueError("v0.1 recording sample representation is fixed")
        if len(set(self.receiver_chain_ids)) != len(self.receiver_chain_ids):
            raise ValueError("receiver chains must be unique")
        segment_ids = {segment.segment_id for segment in self.segments}
        if len(segment_ids) != len(self.segments):
            raise ValueError("segment IDs must be unique")
        activity_segments = [
            sid for activity in self.activities for sid in activity.segment_ids
        ]
        if set(activity_segments) != segment_ids or len(activity_segments) != len(
            segment_ids
        ):
            raise ValueError("each segment must belong to exactly one activity")


@dataclass(frozen=True)
class CapturePlanRef:
    plan_id: PlanId
    plan_digest: Digest


@dataclass(frozen=True)
class CompletedLocalRecording:
    recording_id: RecordingId
    local_locator: str
    manifest: RecordingManifest
    manifest_digest: Digest
    byte_count: int

    def __post_init__(self) -> None:
        require_positive(self.byte_count, "byte_count")
        if self.recording_id != self.manifest.recording_id:
            raise ValueError("recording IDs differ")
