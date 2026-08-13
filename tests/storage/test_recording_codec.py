from __future__ import annotations

import io
import json
from dataclasses import replace

import pytest

from leo_flow.contracts.core import Digest
from leo_flow.contracts.storage import RecordingObjectRef
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.recording_codec import (
    MalformedRecordingError,
    SigMFRecordingObjectReader,
    SigMFRecordingWriter,
)
from testkit import capture_plan, recording_manifest


def write_local(tmp_path, iq: bytes):
    plan = capture_plan()
    manifest = recording_manifest()
    writer = SigMFRecordingWriter()
    session = writer.begin(
        plan,
        manifest.hardware_metadata_snapshot_id,
        str(tmp_path / manifest.recording_id),
    )
    session.append_iq(manifest.segments[0].segment_id, iq)
    session.finish_segment(manifest.segments[0])
    return session.finalize(manifest)


def publish_local(store: FileSystemBlobStore, local) -> RecordingObjectRef:
    with open(local.data_object.locator, "rb") as stream:
        data = store.put(
            stream,
            expected_digest=local.data_object.digest,
            expected_bytes=local.data_object.byte_count,
            media_type="application/octet-stream",
            format_id="leo-recording-data-v1",
            idempotency_key="data",
        )
    with open(local.metadata_object.locator, "rb") as stream:
        metadata = store.put(
            stream,
            expected_digest=local.metadata_object.digest,
            expected_bytes=local.metadata_object.byte_count,
            media_type="application/json",
            format_id="leo-recording-metadata-v1",
            idempotency_key="metadata",
        )
    return RecordingObjectRef(local.recording_id, data, metadata, local.manifest_digest)


def test_writer_reader_round_trip_exact_ci16_and_manifest(tmp_path) -> None:
    iq = bytes(range(64))
    local = write_local(tmp_path / "spool", iq)
    assert local.data_object.byte_count == 64
    assert local.metadata_object.byte_count > 0
    assert not (tmp_path / "spool" / "rec_01.partial").exists()
    store = FileSystemBlobStore(tmp_path / "blobs")
    ref = publish_local(store, local)
    reader = SigMFRecordingObjectReader(store)
    with reader.open(ref) as view:
        assert view.manifest == local.manifest
        assert (
            view.read_iq_bytes(local.manifest.segments[0].segment_id, 1, 3) == iq[8:24]
        )


def test_metadata_embeds_exact_canonical_manifest_value(tmp_path) -> None:
    local = write_local(tmp_path, bytes(range(64)))
    from leo_flow.contracts.core import canonical_digest, canonical_json_bytes

    with open(local.metadata_object.locator, "rb") as stream:
        metadata = json.load(stream)
    assert canonical_json_bytes(metadata["manifest"]) == canonical_json_bytes(
        local.manifest
    )
    assert metadata["manifest_digest"] == str(canonical_digest(local.manifest))


def test_abort_never_creates_complete_pair(tmp_path) -> None:
    plan = capture_plan()
    manifest = recording_manifest()
    destination = tmp_path / manifest.recording_id
    session = SigMFRecordingWriter().begin(
        plan, manifest.hardware_metadata_snapshot_id, str(destination)
    )
    session.append_iq(manifest.segments[0].segment_id, b"\0" * 8)
    session.abort("simulated crash")
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".partial").exists()


def test_writer_rejects_truncated_segment(tmp_path) -> None:
    plan = capture_plan()
    manifest = recording_manifest()
    session = SigMFRecordingWriter().begin(
        plan,
        manifest.hardware_metadata_snapshot_id,
        str(tmp_path / manifest.recording_id),
    )
    session.append_iq(manifest.segments[0].segment_id, b"\0" * 8)
    with pytest.raises(Exception, match="byte count"):
        session.finish_segment(manifest.segments[0])
    session.abort("test")


class IndependentMemoryBlobs:
    """Reader fixture intentionally does not use the production writer."""

    def __init__(self, objects):
        self.objects = objects

    def open(self, ref, byte_range=None):
        from contextlib import nullcontext

        data = self.objects[ref.digest.value]
        if byte_range:
            data = data[byte_range.start : byte_range.stop]
        return nullcontext(io.BytesIO(data))

    def head(self, ref):
        from leo_flow.contracts.storage import ObjectMetadata

        data = self.objects[ref.digest.value]
        if Digest.sha256(data) != ref.digest or len(data) != ref.byte_count:
            raise MalformedRecordingError("bad object")
        return ObjectMetadata(ref, True)


def test_independent_fixture_rejects_overlapping_and_trailing_ranges(tmp_path) -> None:
    del tmp_path
    manifest = recording_manifest()
    from leo_flow.contracts.core import canonical_digest, canonical_json_bytes

    # Constructed from the frozen format spec and contract, not production writer output.
    manifest_wire = json.loads(canonical_json_bytes(manifest))
    raw_meta = {
        "schema": "org.leo-flow.recording-object-metadata",
        "version": "1.0",
        "core:datatype": "ci16_le",
        "leo:namespace_version": "1.0",
        "manifest": manifest_wire,
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
    }
    valid_meta = canonical_json_bytes(raw_meta)
    raw_meta["segments"][0]["byte_offset"] = 1
    malformed = canonical_json_bytes(raw_meta)
    data = bytes(range(64))
    from leo_flow.contracts.storage import ObjectRef

    data_ref = ObjectRef(
        Digest.sha256(data),
        len(data),
        "application/octet-stream",
        "leo-recording-data-v1",
        "memory:data",
    )
    meta_ref = ObjectRef(
        Digest.sha256(malformed),
        len(malformed),
        "application/json",
        "leo-recording-metadata-v1",
        "memory:meta",
    )
    ref = RecordingObjectRef(
        manifest.recording_id, data_ref, meta_ref, canonical_digest(manifest)
    )
    reader = SigMFRecordingObjectReader(
        IndependentMemoryBlobs(
            {data_ref.digest.value: data, meta_ref.digest.value: malformed}
        )
    )
    with pytest.raises(MalformedRecordingError, match="overlap, gap"), reader.open(ref):
        pass
    trailing = data + b"trailing"
    trailing_ref = replace(
        data_ref,
        digest=Digest.sha256(trailing),
        byte_count=len(trailing),
        locator="memory:trailing",
    )
    valid_meta_ref = ObjectRef(
        Digest.sha256(valid_meta),
        len(valid_meta),
        "application/json",
        "leo-recording-metadata-v1",
        "memory:valid-meta",
    )
    ref = RecordingObjectRef(
        manifest.recording_id,
        trailing_ref,
        valid_meta_ref,
        canonical_digest(manifest),
    )
    reader = SigMFRecordingObjectReader(
        IndependentMemoryBlobs(
            {
                trailing_ref.digest.value: trailing,
                valid_meta_ref.digest.value: valid_meta,
            }
        )
    )
    with pytest.raises(MalformedRecordingError, match="trailing"), reader.open(ref):
        pass
