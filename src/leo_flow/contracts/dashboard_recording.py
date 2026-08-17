"""Additive read contracts for one immutable recording capture."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._validation import require_positive, require_token, require_utc_ns
from .capture import ActivityKind, GainMode
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


@dataclass(frozen=True)
class RecordingSegmentViewV0_1:
    """Factual requested/observed tuning values for one captured segment."""

    segment_id: SegmentId
    activity_id: ActivityId
    activity_kind: ActivityKind
    receiver_chain_ids: tuple[ReceiverChainId, ...]
    started_utc_ns: UtcNs
    finished_utc_ns: UtcNs
    center_frequency_hz: float
    sample_rate_hz: float
    bandwidth_hz: float
    gain_mode: GainMode
    gain_db: float | None
    sample_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.activity_kind, ActivityKind):
            raise TypeError("activity_kind must be an ActivityKind")
        if not isinstance(self.gain_mode, GainMode):
            raise TypeError("gain_mode must be a GainMode")
        if not self.receiver_chain_ids or len(set(self.receiver_chain_ids)) != len(
            self.receiver_chain_ids
        ):
            raise ValueError("receiver_chain_ids must be non-empty and unique")
        require_utc_ns(self.started_utc_ns, "started_utc_ns")
        require_utc_ns(self.finished_utc_ns, "finished_utc_ns")
        if self.finished_utc_ns <= self.started_utc_ns:
            raise ValueError("segment interval must be non-empty")
        for name in ("center_frequency_hz", "sample_rate_hz", "bandwidth_hz"):
            require_positive(getattr(self, name), name)
        if self.bandwidth_hz > self.sample_rate_hz:
            raise ValueError("bandwidth_hz cannot exceed sample_rate_hz")
        if (
            isinstance(self.sample_count, bool)
            or not isinstance(self.sample_count, int)
            or self.sample_count <= 0
        ):
            raise ValueError("sample_count must be a positive integer")
        if self.gain_mode is GainMode.MANUAL and self.gain_db is None:
            raise ValueError("manual gain requires gain_db")
        if self.gain_mode is GainMode.AGC and self.gain_db is not None:
            raise ValueError("AGC gain cannot declare gain_db")


@dataclass(frozen=True)
class RecordingCaptureDetailViewV0_1:
    """Bounded manifest facts projected for the operator detail page."""

    schema: SchemaRef
    recording_id: RecordingId
    plan_id: PlanId
    station_id: StationId
    radio_id: RadioId
    radio_serial: str
    hardware_snapshot_id: HardwareSnapshotId
    producer: str
    clock_status: str
    capture_started_utc_ns: UtcNs
    capture_finished_utc_ns: UtcNs
    analysis_state: str
    recording_object_available: bool
    manifest_digest: Digest
    sample_dtype: str
    sample_layout: tuple[str, str, str]
    segments: tuple[RecordingSegmentViewV0_1, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.recording-capture-detail"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported recording capture detail schema")
        for field in ("radio_serial", "producer", "clock_status", "analysis_state"):
            require_token(getattr(self, field), field)
        require_utc_ns(self.capture_started_utc_ns, "capture_started_utc_ns")
        require_utc_ns(self.capture_finished_utc_ns, "capture_finished_utc_ns")
        if self.capture_finished_utc_ns <= self.capture_started_utc_ns:
            raise ValueError("capture interval must be non-empty")
        if not isinstance(self.recording_object_available, bool):
            raise TypeError("recording_object_available must be a boolean")
        if self.sample_dtype != "<i2" or self.sample_layout != (
            "sample",
            "receiver",
            "component",
        ):
            raise ValueError("recording sample representation is unsupported")
        if not self.segments:
            raise ValueError("recording detail requires at least one segment")
        if tuple(sorted(self.segments, key=lambda item: int(item.started_utc_ns))) != (
            self.segments
        ):
            raise ValueError("segments must use chronological order")
        if len({item.segment_id for item in self.segments}) != len(self.segments):
            raise ValueError("segment IDs must be unique")
        if any(
            item.started_utc_ns < self.capture_started_utc_ns
            or item.finished_utc_ns > self.capture_finished_utc_ns
            for item in self.segments
        ):
            raise ValueError("segment intervals must be within the capture interval")


class RecordingCaptureDetailQueryPortV0_1(Protocol):
    """Read one projected recording without opening its CAS objects."""

    def recording_capture_detail(
        self, recording_id: RecordingId
    ) -> RecordingCaptureDetailViewV0_1: ...
