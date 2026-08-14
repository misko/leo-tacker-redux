from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import psycopg
import pytest

from leo_flow.contracts.capture import CompletedLocalRecording, LocalObjectRef
from leo_flow.contracts.core import Digest, canonical_digest, canonical_json_bytes
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.local_recording import RootedSigMFRecordingStore
from leo_flow.storage.postgres_catalog import (
    ObjectCollisionError,
    PostgresRecordingCatalog,
    PostgresRecordingPublisher,
    RecordingConflictError,
    connection_factory,
)
from testkit import object_ref, recording_manifest, recording_object_ref


@pytest.mark.integration
def test_pair_publication_is_one_visible_transaction(postgres_dsn: str) -> None:
    catalog = PostgresRecordingCatalog(connection_factory(postgres_dsn))
    recording = recording_object_ref()
    assert catalog.get(recording.recording_id) is None
    published = catalog.publish(recording, idempotency_key="recording:one")
    assert published.recording_object == recording
    assert catalog.get(recording.recording_id) == published
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM recording").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM object_blob").fetchone() == (2,)


@pytest.mark.integration
def test_completed_local_pair_uploads_then_registers_atomically(
    postgres_dsn: str, tmp_path
) -> None:
    manifest = recording_manifest()
    data_bytes = bytes(range(64))
    metadata_bytes = canonical_json_bytes(manifest)
    recording_root = tmp_path / "local"
    recording_directory = recording_root / str(manifest.recording_id)
    recording_directory.mkdir(parents=True)
    data_path = recording_directory / "recording.data"
    metadata_path = recording_directory / "recording.meta"
    data_path.write_bytes(data_bytes)
    metadata_path.write_bytes(metadata_bytes)
    completed = CompletedLocalRecording(
        manifest.recording_id,
        LocalObjectRef(str(data_path), Digest.sha256(data_bytes), len(data_bytes)),
        LocalObjectRef(
            str(metadata_path), Digest.sha256(metadata_bytes), len(metadata_bytes)
        ),
        manifest,
        canonical_digest(manifest),
    )
    catalog = PostgresRecordingCatalog(connection_factory(postgres_dsn))
    publisher = PostgresRecordingPublisher(
        RootedSigMFRecordingStore(recording_root),
        FileSystemBlobStore(tmp_path / "blobs"),
        catalog,
    )
    published = publisher.publish(completed, idempotency_key="local-pair")
    assert published.recording_id == completed.recording_id
    assert catalog.get(completed.recording_id) == published


@pytest.mark.integration
def test_capture_role_can_publish_without_update_privilege(postgres_dsn: str) -> None:
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=psycopg.rows.dict_row)
        connection.execute("SET ROLE leo_capture")
        return connection

    catalog = PostgresRecordingCatalog(connect)
    recording = recording_object_ref()
    first = catalog.publish(recording, idempotency_key="capture-role")
    assert first.recording_object == recording
    assert catalog.publish(recording, idempotency_key="capture-role") == first


@pytest.mark.integration
def test_object_collision_rolls_back_first_object_and_recording(
    postgres_dsn: str,
) -> None:
    catalog = PostgresRecordingCatalog(connection_factory(postgres_dsn))
    original = recording_object_ref()
    catalog.publish(original, idempotency_key="original")
    new_data = object_ref("new-data")
    colliding_metadata = replace(
        original.metadata_object, locator="opaque:different-location"
    )
    conflicting = replace(
        original,
        recording_id=type(original.recording_id)("rec_02"),
        data_object=new_data,
        metadata_object=colliding_metadata,
    )
    with pytest.raises(ObjectCollisionError):
        catalog.publish(conflicting, idempotency_key="conflicting")
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM recording").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM object_blob").fetchone() == (2,)


@pytest.mark.integration
def test_same_key_is_idempotent_and_different_content_conflicts(
    postgres_dsn: str,
) -> None:
    catalog = PostgresRecordingCatalog(connection_factory(postgres_dsn))
    recording = recording_object_ref()
    first = catalog.publish(recording, idempotency_key="stable-key")
    assert catalog.publish(recording, idempotency_key="stable-key") == first
    with pytest.raises(RecordingConflictError):
        catalog.publish(recording, idempotency_key="different-key")
    changed = replace(recording, metadata_object=object_ref("different-metadata"))
    with pytest.raises(RecordingConflictError):
        catalog.publish(changed, idempotency_key="stable-key")


@pytest.mark.integration
def test_concurrent_same_pair_has_one_exposed_row(postgres_dsn: str) -> None:
    recording = recording_object_ref()

    def publish(_index: int):
        catalog = PostgresRecordingCatalog(connection_factory(postgres_dsn))
        return catalog.publish(recording, idempotency_key="concurrent-key")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(publish, range(8)))
    assert all(result.recording_object == recording for result in results)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM recording").fetchone() == (1,)
