from __future__ import annotations

import json
from dataclasses import replace

import pytest

from leo_flow.contracts.continuity import (
    CaptureProvenance,
    ContinuityStatus,
    RefillMetadata,
    SegmentContinuity,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.recording_codec import (
    SigMFRecordingObjectReader,
    SigMFRecordingWriter,
)
from testkit import capture_plan, recording_manifest
from tests.storage.test_recording_codec import publish_local


def facts() -> RefillMetadata:
    return RefillMetadata(
        0,
        0,
        8,
        3,
        10,
        100,
        1000,
        1100,
        1_700_000_000_000_001_000,
        1_700_000_000_000_001_100,
        20,
        (40.0, 41.0),
        (40.0, 42.0),
        (50.0, 51.0),
        (49.0, 50.0),
    )


def test_continuity_round_trips_in_metadata_not_iq(tmp_path) -> None:
    plan = capture_plan()
    manifest = recording_manifest()
    segment = manifest.segments[0]
    session = SigMFRecordingWriter().begin(
        manifest.recording_id,
        plan,
        manifest.hardware_metadata_snapshot_id,
        str(tmp_path / "local"),
    )
    iq = bytes(range(64))
    observation = facts()
    session.append_refill(segment.segment_id, iq, observation)
    continuity = SegmentContinuity(
        ContinuityStatus.VERIFIED,
        segment.requested.receiver_chain_ids,
        CaptureProvenance("v5", "commit", "0.25", "v3", "metadata=1"),
        (observation,),
    )
    session.record_continuity(segment.segment_id, continuity)
    session.finish_segment(segment)
    local = session.finalize(manifest)
    with open(local.data_object.locator, "rb") as stream:
        assert stream.read() == iq
    with open(local.metadata_object.locator, "rb") as stream:
        raw = json.loads(stream.read())
    assert raw["version"] == "1.1"
    assert raw["continuity"][0]["value"]["refills"][0]["buffer_sequence"] == 10

    store = FileSystemBlobStore(tmp_path / "cas")
    with SigMFRecordingObjectReader(store).open(publish_local(store, local)) as view:
        assert view.continuity(segment.segment_id) == continuity
        assert view.read_iq_bytes(segment.segment_id, 0, 8) == iq


def test_writer_rejects_metadata_offset_mismatch(tmp_path) -> None:
    plan = capture_plan()
    manifest = recording_manifest()
    session = SigMFRecordingWriter().begin(
        manifest.recording_id,
        plan,
        manifest.hardware_metadata_snapshot_id,
        str(tmp_path / "local"),
    )
    with pytest.raises(Exception, match="offset"):
        session.append_refill(
            manifest.segments[0].segment_id,
            bytes(range(64)),
            replace(facts(), segment_sample_offset=1),
        )
    session.abort("test")
