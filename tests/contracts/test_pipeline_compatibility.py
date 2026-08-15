from __future__ import annotations

import base64
import inspect
import json
from dataclasses import fields, replace

import pytest

from leo_flow.application.projection_writers import (
    ModelProjectionCommand,
    validate_model_command,
)
from leo_flow.application.projections import ProjectionInputError
from leo_flow.contracts.core import SchemaRef, SchemaVersion, UtcNs
from leo_flow.contracts.dashboard import (
    FeatureView,
    ModelView,
    RecordingDetail,
    RecordingSummary,
    TimeRangeQuery,
)
from leo_flow.contracts.ports import (
    DashboardQueryPort,
    FeatureSetPublisher,
    ModelFitter,
    ModelPublisher,
    RecordingAnalyzer,
    RecordingPublisher,
)
from leo_flow.dashboard.repository import InMemoryDashboardRepository, InvalidCursor
from tests.projection_writer_fixtures import (
    feature_bundle_and_ref,
    model_bundle_and_ref,
    published_recording,
    recording_manifest,
)


def test_pipeline_ports_expose_only_public_transition_values() -> None:
    transitions = {
        (RecordingPublisher, "publish"): "PublishedRecordingRef",
        (RecordingAnalyzer, "analyze"): "FeatureSetBundle",
        (FeatureSetPublisher, "publish"): "FeatureSetRef",
        (ModelFitter, "fit"): "ModelSnapshotBundle",
        (ModelPublisher, "publish"): "ModelSnapshotRef",
        (DashboardQueryPort, "recent_recordings"): "Page[RecordingSummary]",
        (DashboardQueryPort, "recording_features"): "Page[FeatureView]",
        (DashboardQueryPort, "model_snapshot"): "ModelView",
    }
    forbidden = ("Postgres", "SigMF", "FileSystem", "DashboardRepository")

    for (port, method_name), expected_return in transitions.items():
        signature = inspect.signature(getattr(port, method_name))
        annotations = " ".join(
            str(parameter.annotation) for parameter in signature.parameters.values()
        )
        annotations += f" {signature.return_annotation}"
        assert str(signature.return_annotation) == expected_return
        assert not any(name in annotations for name in forbidden)


@pytest.mark.parametrize("version", (SchemaVersion(0, 2), SchemaVersion(1, 0)))
def test_authoritative_artifacts_reject_incompatible_schema_versions(
    version: SchemaVersion,
) -> None:
    manifest = recording_manifest(1)
    recording = published_recording(manifest)
    features, _ = feature_bundle_and_ref(recording.recording_object, manifest, 1)
    model, _ = model_bundle_and_ref(1)

    for artifact in (manifest, features, model):
        with pytest.raises(ValueError, match="unsupported"):
            replace(artifact, schema=SchemaRef(artifact.SCHEMA_ID, version))


def test_model_projection_rejects_an_inconsistent_published_identity() -> None:
    bundle, published = model_bundle_and_ref(1)
    _, other = model_bundle_and_ref(2)
    inconsistent = replace(
        published,
        model_run_id=other.model_run_id,
    )

    with pytest.raises(ProjectionInputError, match="published reference differ"):
        validate_model_command(ModelProjectionCommand(bundle, inconsistent))


def test_dashboard_read_model_is_a_reduced_public_dto_not_an_authority() -> None:
    public_fields = {
        field.name
        for dto in (RecordingSummary, RecordingDetail, FeatureView, ModelView)
        for field in fields(dto)
    }

    assert not public_fields & {
        "bundle_ref",
        "data_object",
        "metadata_object",
        "locator",
        "provenance",
        "manifest_digest",
    }


def test_dashboard_rejects_an_incompatible_cursor_contract_version() -> None:
    cursor_document = {
        "v": 2,
        "kind": "recordings",
        "query": "0:10:",
        "anchor": -1,
        "after": [1, "rec_example"],
    }
    cursor = (
        base64.urlsafe_b64encode(
            json.dumps(cursor_document, sort_keys=True, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )

    with pytest.raises(InvalidCursor, match="invalid"):
        InMemoryDashboardRepository().recent_recordings(
            TimeRangeQuery(UtcNs(0), UtcNs(10)), cursor
        )
