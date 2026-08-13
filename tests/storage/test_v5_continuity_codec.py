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
from leo_flow.contracts.core import Digest, canonical_digest, canonical_json_bytes
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.recording_codec import (
    MalformedRecordingError,
    SigMFRecordingObjectReader,
    SigMFRecordingWriter,
)
from testkit import capture_plan, recording_manifest
from tests.storage.test_recording_codec import IndependentMemoryBlobs, publish_local


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
    assert raw["version"] == "1.2"
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


def test_gapped_continuity_roundtrip_exposes_only_safe_rf_windows(tmp_path) -> None:
    plan = capture_plan()
    manifest = recording_manifest()
    segment = manifest.segments[0]
    session = SigMFRecordingWriter().begin(
        manifest.recording_id,
        plan,
        manifest.hardware_metadata_snapshot_id,
        str(tmp_path / "gapped"),
    )
    first = replace(
        facts(),
        sample_count=4,
    )
    second = replace(
        facts(),
        refill_index=1,
        segment_sample_offset=4,
        sample_count=4,
        buffer_sequence=12,
        first_sample_sequence=108,
        monotonic_start_ns=1200,
        monotonic_end_ns=1300,
        utc_start_ns=1_700_000_000_000_001_200,
        utc_end_ns=1_700_000_000_000_001_300,
    )
    iq = bytes(range(64))
    session.append_refill(segment.segment_id, iq[:32], first)
    session.append_refill(segment.segment_id, iq[32:], second)
    continuity = SegmentContinuity.from_refills(
        segment.requested.receiver_chain_ids,
        CaptureProvenance("v5", "commit", "0.25", "v3", "metadata=1"),
        (first, second),
    )
    session.record_continuity(segment.segment_id, continuity)
    session.finish_segment(segment)
    local = session.finalize(manifest)
    store = FileSystemBlobStore(tmp_path / "cas")

    with SigMFRecordingObjectReader(store).open(publish_local(store, local)) as view:
        restored = view.continuity(segment.segment_id)
        assert restored == continuity
        assert restored is not None
        assert restored.status is ContinuityStatus.VERIFIED_GAPPED
        assert [
            (span.start_sample, span.stop_sample)
            for span in view.contiguous_rf_spans(segment.segment_id)
        ] == [(0, 4), (4, 8)]
        assert [
            (window.start_sample, window.stop_sample)
            for window in view.iter_safe_windows(segment.segment_id, 3, 2)
        ] == [(0, 3), (1, 4), (4, 7), (5, 8)]


def _independent_refill(index: int, *, source: int, buffer: int) -> dict[str, object]:
    return {
        "refill_index": index,
        "segment_sample_offset": index * 4,
        "sample_count": 4,
        "stream_id": 3,
        "buffer_sequence": buffer,
        "first_sample_sequence": source,
        "monotonic_start_ns": 1000 + index * 200,
        "monotonic_end_ns": 1100 + index * 200,
        "utc_start_ns": 1_700_000_000_000_001_000 + index * 200,
        "utc_end_ns": 1_700_000_000_000_001_100 + index * 200,
        "time_uncertainty_ns": 20,
        "gain_db_start": [40.0, 41.0],
        "gain_db_end": [40.0, 42.0],
        "rssi_db_start": [50.0, 51.0],
        "rssi_db_end": [49.0, 50.0],
        "gain_observation_overflow_count": 0,
        "gain_event_overflow_count": 0,
        "gain_observations": [],
        "flags": [],
    }


def _independent_continuity_wire() -> dict[str, object]:
    return {
        "status": "verified_gapped",
        "receiver_chain_ids": ["rx_a", "rx_b"],
        "provenance": {
            "firmware_release": "v5",
            "firmware_commit": "commit",
            "host_libiio_version": "0.25",
            "metadata_protocol": "v3",
            "capability": "metadata=1",
        },
        "refills": [
            _independent_refill(0, source=100, buffer=10),
            _independent_refill(1, source=108, buffer=12),
        ],
        "gaps": [
            {
                "prior_refill_index": 0,
                "next_refill_index": 1,
                "stored_sample_offset": 4,
                "first_missing_sample_sequence": 104,
                "next_sample_sequence": 108,
                "missing_sample_count": 4,
                "missing_buffer_count": 1,
            }
        ],
    }


def _open_independent_metadata(
    raw_continuity: dict[str, object], *, version: str = "1.2"
):
    manifest = recording_manifest()
    metadata = {
        "schema": "org.leo-flow.recording-object-metadata",
        "version": version,
        "core:datatype": "ci16_le",
        "leo:namespace_version": version,
        "manifest": json.loads(canonical_json_bytes(manifest)),
        "manifest_digest": str(canonical_digest(manifest)),
        "segments": [
            {
                "segment_id": str(manifest.segments[0].segment_id),
                "byte_offset": 0,
                "byte_count": 64,
                "shape": [8, 2, 2],
                "receiver_chain_ids": ["rx_a", "rx_b"],
            }
        ],
        "continuity": [
            {
                "segment_id": str(manifest.segments[0].segment_id),
                "value": raw_continuity,
            }
        ],
    }
    metadata_bytes = canonical_json_bytes(metadata)
    data = bytes(range(64))
    data_ref = ObjectRef(
        Digest.sha256(data),
        len(data),
        "application/octet-stream",
        "leo-recording-data-v1",
        "memory:data",
    )
    metadata_ref = ObjectRef(
        Digest.sha256(metadata_bytes),
        len(metadata_bytes),
        "application/json",
        "leo-recording-metadata-v1",
        "memory:metadata",
    )
    recording_ref = RecordingObjectRef(
        manifest.recording_id,
        data_ref,
        metadata_ref,
        canonical_digest(manifest),
    )
    blobs = IndependentMemoryBlobs(
        {data_ref.digest.value: data, metadata_ref.digest.value: metadata_bytes}
    )
    return SigMFRecordingObjectReader(blobs).open(recording_ref)


@pytest.mark.parametrize(
    "case",
    ["sequence-regression", "stream-change", "offset-error", "overflow", "bad-gap"],
)
def test_independent_malformed_v12_evidence_fails_closed(case: str) -> None:
    raw = _independent_continuity_wire()
    refills = raw["refills"]
    assert isinstance(refills, list)
    second = refills[1]
    assert isinstance(second, dict)
    if case == "sequence-regression":
        second["first_sample_sequence"] = 103
    elif case == "stream-change":
        second["stream_id"] = 4
    elif case == "offset-error":
        second["segment_sample_offset"] = 5
    elif case == "overflow":
        second["flags"] = ["device_iio_overflow"]
    else:
        gaps = raw["gaps"]
        assert isinstance(gaps, list) and isinstance(gaps[0], dict)
        gaps[0]["missing_sample_count"] = 3

    with (
        pytest.raises(MalformedRecordingError, match="invalid continuity metadata"),
        _open_independent_metadata(raw),
    ):
        pass


def test_independent_legacy_v11_verified_decodes_as_verified_contiguous() -> None:
    raw = _independent_continuity_wire()
    raw["status"] = "verified"
    raw.pop("gaps")
    refills = raw["refills"]
    assert isinstance(refills, list) and isinstance(refills[1], dict)
    refills[1]["buffer_sequence"] = 11
    refills[1]["first_sample_sequence"] = 104

    with _open_independent_metadata(raw, version="1.1") as view:
        continuity = view.continuity(view.manifest.segments[0].segment_id)
    assert continuity is not None
    assert continuity.status is ContinuityStatus.VERIFIED_CONTIGUOUS
