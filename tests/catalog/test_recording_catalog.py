from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.storage.catalog import (
    InMemoryRecordingCatalog,
    RecordingConflictError,
    RecordingPublisherAdapter,
)
from leo_flow.storage.filesystem import IdempotencyConflictError
from testkit import completed_local_recording, object_ref, recording_object_ref


def test_pair_is_the_only_atomic_visibility_boundary() -> None:
    catalog = InMemoryRecordingCatalog()
    recording = recording_object_ref()
    assert catalog.get(str(recording.recording_id)) is None
    published = catalog.publish(recording, idempotency_key="publish:1")
    assert published.recording_object == recording
    assert catalog.get(str(recording.recording_id)) == published


def test_publication_is_idempotent_and_conflicts_are_hard() -> None:
    catalog = InMemoryRecordingCatalog()
    recording = recording_object_ref()
    first = catalog.publish(recording, idempotency_key="stable")
    assert catalog.publish(recording, idempotency_key="stable") == first
    changed = replace(recording, metadata_object=object_ref("changed-meta"))
    with pytest.raises(IdempotencyConflictError):
        catalog.publish(changed, idempotency_key="stable")
    with pytest.raises(RecordingConflictError):
        catalog.publish(changed, idempotency_key="different-key")


def test_failed_second_blob_upload_exposes_no_recording(tmp_path) -> None:
    completed = completed_local_recording()
    data_path = tmp_path / "data"
    data_path.write_bytes(b"data")
    metadata_path = tmp_path / "metadata"
    metadata_path.write_bytes(b"metadata")
    from leo_flow.contracts.capture import LocalObjectRef
    from testkit import digest

    completed = replace(
        completed,
        data_object=LocalObjectRef(str(data_path), digest("data"), 4),
        metadata_object=LocalObjectRef(str(metadata_path), digest("metadata"), 8),
    )

    class FailSecondBlob:
        def __init__(self):
            self.calls = 0

        def put(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise OSError("simulated metadata upload outage")
            return object_ref("uploaded-data")

    catalog = InMemoryRecordingCatalog()
    publisher = RecordingPublisherAdapter(FailSecondBlob(), catalog)
    with pytest.raises(OSError, match="outage"):
        publisher.publish(completed, idempotency_key="publish:failure")
    assert catalog.get(str(completed.recording_id)) is None
