from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from leo_flow.capture.engine import CaptureIdentity, PlanCaptureEngine
from leo_flow.capture.spool import SQLiteLocalSpool
from leo_flow.contracts.capture import (
    ActivityKind,
    ActivityRequest,
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
    HardwareSnapshotId,
    PlanId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    StationId,
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)
from leo_flow.contracts.continuity import RefillMetadata, SegmentContinuity
from testkit import FakeClock

RECORDING_ID = RecordingId("rec_01J00000000000000000000000")
RADIO_ID = RadioId("radio_test")
RECEIVERS = (ReceiverChainId("rx_a"), ReceiverChainId("rx_b"))


def ci16(samples: int, *, start: int = 0, receivers: int = 2) -> bytes:
    values: list[int] = []
    for sample in range(start, start + samples):
        for receiver in range(receivers):
            values.extend((sample * 10 + receiver * 2, sample * 10 + receiver * 2 + 1))
    return struct.pack(f"<{len(values)}h", *values)


def segment(segment_id: str, samples: int = 4) -> SegmentRequest:
    return SegmentRequest(
        segment_id=SegmentId(segment_id),
        center_frequency_hz=11_325_000_000.0,
        sample_rate_hz=2_500_000.0,
        bandwidth_hz=2_500_000.0,
        receiver_chain_ids=RECEIVERS,
        gain=GainSetting(GainMode.MANUAL, 50.0),
        sample_count=samples,
    )


def plan_with_activities(
    activities: tuple[ActivityRequest, ...] | None = None,
) -> CapturePlan:
    if activities is None:
        activities = (
            ActivityRequest(
                ActivityId("act_dwell"), ActivityKind.DWELL, (segment("seg_dwell"),)
            ),
        )
    return CapturePlan(
        SchemaRef(CapturePlan.SCHEMA_ID),
        PlanId("plan_test"),
        RADIO_ID,
        RECEIVERS,
        activities,
    )


class FakeWriteSession:
    def __init__(self, recording_id: RecordingId, destination: str) -> None:
        self._recording_id = recording_id
        self.destination = destination
        self.blocks: dict[SegmentId, list[bytes]] = {}
        self.finished: list[SegmentManifest] = []
        self.aborted_reason: str | None = None
        self.continuities: dict[SegmentId, SegmentContinuity] = {}

    @property
    def recording_id(self) -> RecordingId:
        return self._recording_id

    def append_iq(self, segment_id: SegmentId, ci16_bytes: bytes) -> None:
        self.blocks.setdefault(segment_id, []).append(bytes(ci16_bytes))

    def append_refill(
        self, segment_id: SegmentId, ci16_bytes: bytes, metadata: RefillMetadata
    ) -> None:
        self.append_iq(segment_id, ci16_bytes)

    def record_continuity(
        self, segment_id: SegmentId, continuity: SegmentContinuity
    ) -> None:
        self.continuities[segment_id] = continuity

    def finish_segment(self, segment: SegmentManifest) -> None:
        expected = segment.sample_count * segment.shape[1] * segment.shape[2] * 2
        actual = sum(len(block) for block in self.blocks.get(segment.segment_id, ()))
        if actual != expected:
            raise ValueError("writer observed the wrong byte count")
        self.finished.append(segment)

    def finalize(self, manifest: RecordingManifest) -> CompletedLocalRecording:
        data = b"".join(
            block
            for segment_manifest in self.finished
            for block in self.blocks[segment_manifest.segment_id]
        )
        metadata = canonical_json_bytes(manifest)
        return CompletedLocalRecording(
            self.recording_id,
            LocalObjectRef(f"{self.destination}/data", Digest.sha256(data), len(data)),
            LocalObjectRef(
                f"{self.destination}/metadata",
                Digest.sha256(metadata),
                len(metadata),
            ),
            manifest,
            canonical_digest(manifest),
        )

    def abort(self, reason: str) -> None:
        self.aborted_reason = reason


class FakeRecordingWriter:
    def __init__(self, recording_id_override: RecordingId | None = None) -> None:
        self.recording_id = recording_id_override or RECORDING_ID
        self._recording_id_override = recording_id_override
        self.session: FakeWriteSession | None = None

    def begin(
        self,
        recording_id: RecordingId,
        plan: CapturePlan,
        hardware_metadata_snapshot_id: HardwareSnapshotId,
        destination: str,
    ) -> FakeWriteSession:
        del plan, hardware_metadata_snapshot_id
        self.recording_id = self._recording_id_override or recording_id
        self.session = FakeWriteSession(self.recording_id, destination)
        return self.session


def spool(tmp_path: Path) -> SQLiteLocalSpool:
    return SQLiteLocalSpool(
        tmp_path / "capture.sqlite3",
        tmp_path / "recordings",
        id_factory=lambda: RECORDING_ID,
        now_ns=lambda: 1_700_000_000_000_000_000,
    )


def engine(clock: FakeClock) -> PlanCaptureEngine:
    return PlanCaptureEngine(
        CaptureIdentity(
            StationId("station_test"),
            "serial-test",
            "locked",
            HardwareSnapshotId("hw_test"),
            "capture-test",
        ),
        clock=clock,
        delay=lambda seconds: clock.advance_ns(round(seconds * 1e9)),
    )


@dataclass
class FakePublisher:
    failures_remaining: int = 0
    calls: list[tuple[RecordingId, str]] = field(default_factory=list)

    def publish(
        self, recording: CompletedLocalRecording, *, idempotency_key: str
    ) -> PublishedRecordingRef:
        self.calls.append((recording.recording_id, idempotency_key))
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise OSError("offline")
        return PublishedRecordingRef(
            RecordingObjectRef(
                recording.recording_id,
                _published_object(recording.data_object, "sigmf-data-v1"),
                _published_object(recording.metadata_object, "sigmf-meta-v1"),
                recording.manifest_digest,
            )
        )


def _published_object(local: LocalObjectRef, format_id: str) -> ObjectRef:
    return ObjectRef(
        local.digest,
        local.byte_count,
        "application/octet-stream",
        format_id,
        f"published:{local.digest.value}",
    )


@dataclass
class FakeCleaner:
    fail: bool = False
    calls: list[RecordingId] = field(default_factory=list)

    def cleanup(self, recording: CompletedLocalRecording) -> None:
        self.calls.append(recording.recording_id)
        if self.fail:
            raise OSError("local filesystem busy")
