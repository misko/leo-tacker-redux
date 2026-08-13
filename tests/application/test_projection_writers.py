from __future__ import annotations

from dataclasses import fields, replace

import pytest

from leo_flow.application.projection_writers import (
    FeatureProjectionCommand,
    RecordingProjectionCommand,
    TrackProjectionCommand,
    validate_feature_command,
    validate_recording_command,
    validate_storage_health,
    validate_track_command,
)
from leo_flow.application.projections import ProjectionInputError
from leo_flow.contracts.core import (
    Digest,
    ModelSnapshotId,
    RadioId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.dashboard import StorageHealth, TrackView
from tests.projection_writer_fixtures import (
    feature_bundle_and_ref,
    model_bundle_and_ref,
    published_recording,
    recording_manifest,
)


def test_recording_command_closes_over_published_manifest() -> None:
    manifest = recording_manifest(1)
    published = published_recording(manifest)
    validate_recording_command(RecordingProjectionCommand(manifest, published, True))
    assert {field.name for field in fields(RecordingProjectionCommand)} == {
        "manifest",
        "published_ref",
        "recording_object_available",
    }
    wrong = published_recording(recording_manifest(2))
    with pytest.raises(ProjectionInputError, match="published recording IDs"):
        validate_recording_command(RecordingProjectionCommand(manifest, wrong, True))


def test_feature_command_closes_over_bundle_and_recording_identity() -> None:
    manifest = recording_manifest(1)
    recording = published_recording(manifest)
    bundle, ref = feature_bundle_and_ref(recording.recording_object, manifest, 1)
    validate_feature_command(FeatureProjectionCommand(bundle, ref, recording))
    wrong_bundle = replace(
        bundle, input_recording_identity_digest=ref.bundle_ref.digest
    )
    payload = canonical_json_bytes(wrong_bundle)
    wrong_ref = replace(
        ref,
        bundle_ref=replace(
            ref.bundle_ref, digest=Digest.sha256(payload), byte_count=len(payload)
        ),
    )
    with pytest.raises(ProjectionInputError, match="identify the recording"):
        validate_feature_command(
            FeatureProjectionCommand(wrong_bundle, wrong_ref, recording)
        )


def test_track_and_storage_are_validated_before_adapter_use() -> None:
    model, model_ref = model_bundle_and_ref(1)
    with pytest.raises(ProjectionInputError, match="interval"):
        validate_track_command(
            TrackProjectionCommand(
                TrackView(
                    "track_bad",
                    model.model_snapshot_id,
                    UtcNs(20),
                    UtcNs(10),
                ),
                RadioId("radio_test"),
                model,
                model_ref,
            )
        )
    with pytest.raises(ProjectionInputError, match="both"):
        validate_storage_health(StorageHealth(True, 100, None))
    with pytest.raises(ProjectionInputError, match="references differ"):
        validate_track_command(
            TrackProjectionCommand(
                TrackView(
                    "track_bad_model",
                    ModelSnapshotId("model_other"),
                    UtcNs(10),
                    UtcNs(20),
                ),
                RadioId("radio_test"),
                model,
                model_ref,
            )
        )
