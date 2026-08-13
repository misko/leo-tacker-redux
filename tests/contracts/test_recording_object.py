from __future__ import annotations

import inspect
from dataclasses import replace

import pytest

from leo_flow.contracts.core import (
    ArtifactRef,
    RecordingId,
    SchemaRef,
)
from leo_flow.contracts.features import RecordingAnalysisRequest
from leo_flow.contracts.storage import PublishedRecordingRef, RecordingObjectRef
from leo_flow.storage.ports import RecordingObjectReader
from testkit import completed_local_recording, digest, recording_object_ref


def test_recording_object_is_an_required_data_metadata_pair() -> None:
    recording = recording_object_ref()
    assert recording.recording_id == RecordingId("rec_01")
    assert recording.data_object.digest != recording.metadata_object.digest
    assert recording.manifest_digest == digest("embedded-manifest")
    published = PublishedRecordingRef(recording)
    assert published.recording_id == recording.recording_id


def test_logical_identity_is_stable_across_storage_relocation() -> None:
    recording = recording_object_ref()
    relocated = replace(
        recording,
        data_object=replace(recording.data_object, locator="opaque:new-data-location"),
        metadata_object=replace(
            recording.metadata_object, locator="opaque:new-metadata-location"
        ),
    )
    assert relocated.identity_digest() == recording.identity_digest()
    changed_metadata = replace(
        recording,
        metadata_object=replace(
            recording.metadata_object, digest=digest("different-metadata")
        ),
    )
    assert changed_metadata.identity_digest() != recording.identity_digest()


def test_pair_rejects_one_blob_used_as_both_roles() -> None:
    recording = recording_object_ref()
    with pytest.raises(ValueError, match="distinct"):
        replace(recording, metadata_object=recording.data_object)


def test_completed_local_recording_carries_both_verified_objects() -> None:
    completed = completed_local_recording()
    assert completed.data_object.locator == "local:data"
    assert completed.metadata_object.locator == "local:metadata"
    assert completed.data_object.byte_count == 128
    assert completed.metadata_object.byte_count == 128
    with pytest.raises(ValueError, match="distinct"):
        replace(completed, metadata_object=completed.data_object)


def test_analysis_request_pins_the_whole_recording_pair() -> None:
    recording = recording_object_ref()
    request = RecordingAnalysisRequest(
        SchemaRef(RecordingAnalysisRequest.SCHEMA_ID),
        recording.recording_id,
        recording,
        ArtifactRef("algorithm_test-v1", digest("algorithm")),
        ArtifactRef("config_test-v1", digest("config")),
        (),
        SchemaRef("org.leo-flow.feature-set-bundle"),
    )
    assert request.recording_object_ref is recording
    with pytest.raises(ValueError, match="recording IDs differ"):
        replace(request, recording_id=RecordingId("rec_02"))


def test_recording_reader_accepts_one_logical_ref_not_individual_blobs() -> None:
    parameters = inspect.signature(RecordingObjectReader.open).parameters
    assert tuple(parameters) == ("self", "recording_ref")
    assert "raw_ref" not in parameters
    assert "manifest_digest" not in parameters


def test_scientific_contract_does_not_name_the_selected_physical_codec() -> None:
    names = " ".join(
        (*RecordingObjectRef.__annotations__, *RecordingAnalysisRequest.__annotations__)
    ).lower()
    assert "sigmf" not in names
