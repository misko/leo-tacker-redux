from __future__ import annotations

import json
from dataclasses import replace

import pytest

from leo_flow.application import DashboardProjectionStore, ProjectionInputError
from leo_flow.application.projection_writers import (
    FeatureProjectionCommand,
    ModelProjectionCommand,
    RecordingProjectionCommand,
    validate_feature_command,
    validate_model_command,
    validate_recording_command,
)
from leo_flow.contracts.core import (
    DetectorEvaluationId,
    Digest,
    EvaluationRunId,
    ModelRunId,
    SchemaRef,
    SchemaVersion,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.dashboard import TimeRangeQuery
from leo_flow.contracts.evaluation import (
    DetectorEvaluationRef,
    DetectorEvaluationView,
    DetectorMethodSplitSummary,
)
from leo_flow.contracts.model import feature_dataset_membership_digest
from leo_flow.contracts.ports import DashboardQueryPort
from leo_flow.contracts.storage import ObjectRef
from leo_flow.dashboard import DashboardJsonApplication, JsonRequest
from tests.projection_writer_fixtures import (
    feature_bundle_and_ref,
    model_bundle_and_ref,
    published_recording,
    recording_manifest,
)


def _response(
    application: DashboardJsonApplication,
    path: str,
    query: dict[str, str] | None = None,
) -> tuple[int, dict]:
    result = application.handle(JsonRequest("GET", path, query or {}))
    return result.status, json.loads(result.body)


def _project_recording(
    projections: DashboardProjectionStore,
    command: RecordingProjectionCommand,
    *,
    analysis_state: str,
) -> None:
    validate_recording_command(command)
    projections.project_recording(
        command.manifest,
        recording_object_available=command.recording_object_available,
        analysis_state=analysis_state,
    )


def _project_feature(
    projections: DashboardProjectionStore, command: FeatureProjectionCommand
) -> None:
    validate_feature_command(command)
    projections.project_features(command.bundle)


def _project_model(
    projections: DashboardProjectionStore, command: ModelProjectionCommand
) -> None:
    validate_model_command(command)
    projections.project_model(command.bundle, command.published_ref)


def _evaluation(membership_digest: Digest) -> DetectorEvaluationView:
    report_bytes = canonical_json_bytes(
        {
            "schema_id": "org.leo-flow.detector-evaluation-report",
            "schema_version": "0.1",
            "feature_membership_digest": str(membership_digest),
        }
    )
    report_digest = Digest.sha256(report_bytes)
    report = ObjectRef(
        report_digest,
        len(report_bytes),
        "application/json",
        "detector-evaluation-report-v0.1",
        f"cas:sha256:{report_digest.value}",
    )
    return DetectorEvaluationView(
        DetectorEvaluationRef(
            DetectorEvaluationId("eval_projection_e2e"),
            EvaluationRunId("erun_projection_e2e"),
            report_digest,
            report,
        ),
        "dataset_projection_e2e",
        Digest.sha256(b"dataset-snapshot"),
        membership_digest,
        "threshold_projection_e2e",
        Digest.sha256(b"threshold-rule"),
        "dataset_calibration",
        "validation",
        1,
        2,
        (),
        (
            DetectorMethodSplitSummary(
                "fft-energy@1",
                "validation",
                0.5,
                "normalized-energy",
                2,
                2,
                2,
                2,
                0,
                1,
                1,
                0,
                1,
                0,
                2,
                0,
            ),
        ),
    )


def test_public_artifacts_project_to_reduced_read_model_and_replay_exactly() -> None:
    first_manifest = recording_manifest(1)
    second_manifest = recording_manifest(2)
    first_recording = published_recording(first_manifest)
    second_recording = published_recording(second_manifest)
    first_features = feature_bundle_and_ref(
        first_recording.recording_object, first_manifest, 1
    )
    second_features = feature_bundle_and_ref(
        second_recording.recording_object, second_manifest, 2
    )
    membership = feature_dataset_membership_digest(
        (first_features[1], second_features[1])
    )
    base_model, base_model_ref = model_bundle_and_ref(1)
    model = replace(
        base_model,
        dataset_membership_digest=membership,
        provenance=replace(base_model.provenance, input_digests=(membership,)),
    )
    model_bytes = canonical_json_bytes(model)
    model_ref = replace(
        base_model_ref,
        bundle_ref=replace(
            base_model_ref.bundle_ref,
            digest=Digest.sha256(model_bytes),
            byte_count=len(model_bytes),
        ),
    )
    evaluation = _evaluation(membership)
    recording_commands = (
        RecordingProjectionCommand(first_manifest, first_recording, True),
        RecordingProjectionCommand(second_manifest, second_recording, True),
    )
    feature_commands = (
        FeatureProjectionCommand(first_features[0], first_features[1], first_recording),
        FeatureProjectionCommand(
            second_features[0], second_features[1], second_recording
        ),
    )
    model_command = ModelProjectionCommand(model, model_ref)
    projections = DashboardProjectionStore()

    for command in recording_commands:
        _project_recording(projections, command, analysis_state="complete")
    for command in feature_commands:
        _project_feature(projections, command)
    _project_model(projections, model_command)
    projections.project_evaluation(evaluation)

    queries: DashboardQueryPort = projections.repository()
    query = TimeRangeQuery(UtcNs(0), UtcNs(10_000))
    first_view = (
        queries.recent_recordings(query),
        queries.recording_features(first_manifest.recording_id, "*"),
        queries.model_snapshot(str(model.model_snapshot_id)),
        queries.detector_evaluation(str(evaluation.ref.evaluation_id)),
    )
    assert first_view[0].items[0].analysis_state == "complete"
    assert first_view[1].items[0].feature_id == "feature_projection_1"
    assert first_view[2].model_snapshot_id == model.model_snapshot_id
    assert first_view[3].feature_membership_digest == membership
    assert not queries.storage_health().available

    # Replaying identical authorities can append projection versions, but the
    # public read model and immutable identities remain exactly idempotent.
    for command in recording_commands:
        _project_recording(projections, command, analysis_state="complete")
    for command in feature_commands:
        _project_feature(projections, command)
    _project_model(projections, model_command)
    projections.project_evaluation(evaluation)
    replayed: DashboardQueryPort = projections.repository()
    assert (
        replayed.recent_recordings(query),
        replayed.recording_features(first_manifest.recording_id, "*"),
        replayed.model_snapshot(str(model.model_snapshot_id)),
        replayed.detector_evaluation(str(evaluation.ref.evaluation_id)),
    ) == first_view

    application = projections.json_application()
    status, evaluation_payload = _response(
        application, f"/api/evaluations/{evaluation.ref.run_id}"
    )
    assert status == 200
    assert evaluation_payload["evaluation_id"] == str(evaluation.ref.evaluation_id)
    assert evaluation_payload["schema_version"] == 1
    assert "input_recording_identity_digest" not in json.dumps(evaluation_payload)
    assert _response(application, "/api/storage-health") == (
        200,
        {"available": False, "free_bytes": None, "total_bytes": None},
    )


def test_projection_failures_are_closed_before_reduced_rows_exist() -> None:
    first_manifest = recording_manifest(1)
    second_manifest = recording_manifest(2)
    first_recording = published_recording(first_manifest)
    second_recording = published_recording(second_manifest)
    bundle, feature_ref = feature_bundle_and_ref(
        first_recording.recording_object, first_manifest, 1
    )
    projections = DashboardProjectionStore()

    with pytest.raises(ProjectionInputError, match="published recording IDs"):
        validate_recording_command(
            RecordingProjectionCommand(first_manifest, second_recording, True)
        )
    _project_recording(
        projections,
        RecordingProjectionCommand(first_manifest, first_recording, True),
        analysis_state="pending",
    )
    with pytest.raises(ProjectionInputError, match="recording references differ"):
        _project_feature(
            projections,
            FeatureProjectionCommand(bundle, feature_ref, second_recording),
        )
    base_model, base_model_ref = model_bundle_and_ref(1)
    with pytest.raises(ProjectionInputError, match="published reference differ"):
        _project_model(
            projections,
            ModelProjectionCommand(
                base_model,
                replace(base_model_ref, model_run_id=ModelRunId("mrun_mismatch")),
            ),
        )
    for incompatible in (
        SchemaRef("org.leo-flow.unknown-feature-result"),
        SchemaRef(bundle.SCHEMA_ID, SchemaVersion(1, 0)),
    ):
        with pytest.raises(ValueError, match="unsupported"):
            replace(bundle, schema=incompatible)
    evaluation = _evaluation(bundle.input_recording_identity_digest)
    with pytest.raises(ValueError, match="must match"):
        replace(
            evaluation.ref,
            report_digest=Digest.sha256(b"another-evaluation-report"),
        )
    unsupported_evaluation = replace(
        evaluation,
        ref=replace(
            evaluation.ref,
            report_object=replace(
                evaluation.ref.report_object,
                format_id="detector-evaluation-report-v9.0",
            ),
        ),
    )
    with pytest.raises(ProjectionInputError, match="unsupported format"):
        projections.project_evaluation(unsupported_evaluation)

    application = projections.json_application()
    status, detail = _response(
        application, f"/api/recordings/{first_manifest.recording_id}"
    )
    assert status == 200
    assert detail["summary"]["analysis_state"] == "pending"
    assert (
        _response(
            application,
            f"/api/recordings/{first_manifest.recording_id}/features",
            {"selector": "*"},
        )[1]["items"]
        == []
    )
    assert _response(application, "/api/models/model_projection_1")[0] == 404
    assert _response(application, "/api/evaluations/eval_projection_e2e")[0] == 404


def test_cursor_snapshot_is_stable_and_stale_reuse_fails_closed() -> None:
    projections = DashboardProjectionStore()
    first = recording_manifest(1)
    second = recording_manifest(2)
    for manifest in (first, second):
        published = published_recording(manifest)
        _project_recording(
            projections,
            RecordingProjectionCommand(manifest, published, True),
            analysis_state="complete",
        )
    query = TimeRangeQuery(UtcNs(0), UtcNs(10_000))
    repository = projections.repository(page_size=1)
    page = repository.recent_recordings(query)
    assert page.next_cursor is not None

    later = recording_manifest(3)
    later_published = published_recording(later)
    _project_recording(
        projections,
        RecordingProjectionCommand(later, later_published, True),
        analysis_state="pending",
    )
    continuation = repository.recent_recordings(query, page.next_cursor)
    assert [item.recording_id for item in continuation.items] == [first.recording_id]

    incompatible_query = TimeRangeQuery(UtcNs(0), UtcNs(5_000))
    with pytest.raises(ValueError, match="invalid"):
        repository.recent_recordings(incompatible_query, page.next_cursor)
