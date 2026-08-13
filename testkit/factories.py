"""Small factories for independently constructing boundary values."""

from __future__ import annotations

from leo_flow.contracts.capture import (
    ActivityKind,
    ActivityManifest,
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
    UtcNs,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef


def digest(seed: str = "fixture") -> Digest:
    return Digest.sha256(seed.encode())


def object_ref(seed: str = "fixture") -> ObjectRef:
    return ObjectRef(
        digest(seed), 128, "application/octet-stream", "fixture-v1", f"opaque:{seed}"
    )


def recording_object_ref() -> RecordingObjectRef:
    return RecordingObjectRef(
        RecordingId("rec_01"),
        object_ref("recording-data"),
        object_ref("recording-metadata"),
        digest("embedded-manifest"),
    )


def completed_local_recording() -> CompletedLocalRecording:
    manifest = recording_manifest()
    return CompletedLocalRecording(
        manifest.recording_id,
        LocalObjectRef("local:data", digest("recording-data"), 128),
        LocalObjectRef("local:metadata", digest("recording-metadata"), 128),
        manifest,
        digest("embedded-manifest"),
    )


def capture_plan() -> CapturePlan:
    segment = SegmentRequest(
        SegmentId("seg_01"),
        11_325_000_000.0,
        2_500_000.0,
        2_500_000.0,
        (ReceiverChainId("rx_a"), ReceiverChainId("rx_b")),
        GainSetting(GainMode.MANUAL, 50.0),
        sample_count=8,
    )
    return CapturePlan(
        SchemaRef(CapturePlan.SCHEMA_ID),
        PlanId("plan_01"),
        RadioId("radio_01"),
        segment.receiver_chain_ids,
        (ActivityRequest(ActivityId("act_01"), ActivityKind.DWELL, (segment,)),),
    )


def recording_manifest() -> RecordingManifest:
    plan = capture_plan()
    requested = plan.activities[0].segments[0]
    segment = SegmentManifest(
        requested.segment_id,
        requested,
        requested.center_frequency_hz,
        requested.sample_rate_hz,
        requested.bandwidth_hz,
        requested.gain,
        UtcNs(1_700_000_000_100_000_000),
        100,
        8,
        (8, 2, 2),
    )
    activity = ActivityManifest(
        plan.activities[0].activity_id,
        ActivityKind.DWELL,
        UtcNs(1_700_000_000_100_000_000),
        UtcNs(1_700_000_001_100_000_000),
        (segment.segment_id,),
    )
    return RecordingManifest(
        SchemaRef(RecordingManifest.SCHEMA_ID),
        RecordingId("rec_01"),
        UtcNs(1_700_000_000_000_000_000),
        UtcNs(1_700_000_000_100_000_000),
        UtcNs(1_700_000_001_100_000_000),
        StationId("station_01"),
        plan.radio_id,
        "serial-01",
        plan.receiver_chain_ids,
        "locked",
        HardwareSnapshotId("hw_01"),
        (activity,),
        (segment,),
        plan.plan_id,
        "fixture-producer",
    )
