from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest
from integration._model_fixtures import (
    FeatureReader,
    HardwareReader,
    NoEphemerides,
    dataset,
    digest,
    execution_context,
    feature_set,
    hardware,
    model_request,
    recording_manifest,
)

from leo_flow.analysis.model import (
    ReceiverQualityAggregateConfig,
    ReceiverQualityAggregateModel,
)
from leo_flow.application import (
    DashboardProjectionStore,
    InMemoryModelPublication,
    ModelPublicationConflict,
    ModelPublicationError,
    ProjectionInputError,
)
from leo_flow.contracts.capture import ActivityKind
from leo_flow.contracts.core import (
    Digest,
    ModelRunId,
    ModelSnapshotId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.model import (
    ModelApproval,
    ModelSnapshotRef,
)
from leo_flow.contracts.ports import ModelPublisher, ModelReleasePublisher
from leo_flow.contracts.storage import ObjectRef

APPLICATION = Path(__file__).resolve().parents[2] / "src" / "leo_flow" / "application"


def fitted_model():
    dwell = recording_manifest(0, kind=ActivityKind.DWELL, started_utc_ns=1_000)
    scan = recording_manifest(1, kind=ActivityKind.SCAN, started_utc_ns=3_000)
    first = feature_set(dwell, 10.0)
    second = feature_set(scan, 20.0)
    snapshot = dataset((first[0], second[0]))
    hardware_ref, hardware_snapshot = hardware()
    config = ReceiverQualityAggregateConfig()
    request = model_request(snapshot, config, hardware_ref)
    bundle = ReceiverQualityAggregateModel(snapshot, config, execution_context()).fit(
        request,
        FeatureReader((first, second)),
        NoEphemerides(),
        HardwareReader(hardware_ref, hardware_snapshot),
    )
    return request, bundle, dwell, first[1]


def test_model_adapter_structurally_satisfies_both_frozen_publication_ports() -> None:
    adapter = InMemoryModelPublication()
    publisher: ModelPublisher = adapter
    releaser: ModelReleasePublisher = adapter
    assert publisher is releaser


def test_model_publish_and_release_are_idempotent_and_alias_is_explicit() -> None:
    request, bundle, _, _ = fitted_model()
    adapter = InMemoryModelPublication()
    first_publish = adapter.publish(request, bundle, idempotency_key="publish-model")
    assert adapter.bundle(first_publish) == bundle
    assert (
        adapter.publish(request, bundle, idempotency_key="publish-model")
        == first_publish
    )
    approval = ModelApproval(
        approved_by="reviewer",
        approved_utc_ns=UtcNs(100),
        rationale="reviewed fixture",
    )
    first_release = adapter.release(
        first_publish, "current", approval, idempotency_key="release-model"
    )
    assert (
        adapter.release(
            first_publish, "current", approval, idempotency_key="release-model"
        )
        == first_release
    )
    replacement = adapter.release(
        first_publish,
        "current",
        replace(approval, rationale="a different approval record"),
        idempotency_key="another-release",
    )
    assert adapter.get_release("current") == replacement
    with pytest.raises(ModelPublicationConflict, match="idempotency key"):
        adapter.release(
            first_publish,
            "candidate",
            approval,
            idempotency_key="release-model",
        )


def test_publication_validates_bundle_and_exact_published_release_ref() -> None:
    request, bundle, _, _ = fitted_model()
    adapter = InMemoryModelPublication()
    invalid = replace(bundle, dataset_membership_digest=digest("wrong-membership"))
    with pytest.raises(ModelPublicationError, match="membership"):
        adapter.publish(
            request,
            invalid,
            idempotency_key="bad-projection",
        )
    payload = canonical_json_bytes(bundle)
    object_ref = ObjectRef(
        Digest.sha256(payload),
        len(payload),
        "application/json",
        "model-snapshot-bundle-v0.1",
        f"memory://models/{Digest.sha256(payload).value}",
    )
    unpublished = ModelSnapshotRef(
        bundle.model_snapshot_id, bundle.model_run_id, object_ref
    )
    with pytest.raises(ModelPublicationError, match="exactly published"):
        adapter.release(
            unpublished,
            "current",
            ModelApproval("reviewer", UtcNs(100), "not published"),
            idempotency_key="bad-release",
        )
    with pytest.raises(ModelPublicationError, match="idempotency_key"):
        adapter.publish(
            request,
            bundle,
            idempotency_key="",
        )


def test_publication_rejects_reused_keys_and_model_ids_with_different_objects() -> None:
    request, bundle, _, _ = fitted_model()
    adapter = InMemoryModelPublication()
    original = adapter.publish(request, bundle, idempotency_key="original")

    another_identity = replace(
        bundle, model_snapshot_id=ModelSnapshotId("model_another")
    )
    with pytest.raises(ModelPublicationConflict, match="idempotency key"):
        adapter.publish(
            request,
            another_identity,
            idempotency_key="original",
        )

    another_run = replace(bundle, model_run_id=ModelRunId("mrun_another"))
    with pytest.raises(ModelPublicationConflict, match="snapshot ID"):
        adapter.publish(
            request,
            another_run,
            idempotency_key="another-run",
        )
    with pytest.raises(ModelPublicationError, match="exactly published"):
        adapter.bundle(replace(original, model_run_id=ModelRunId("mrun_wrong")))


def test_projection_rejects_orphans_and_release_before_draft_publication() -> None:
    request, bundle, manifest, features = fitted_model()
    projections = DashboardProjectionStore()
    with pytest.raises(ProjectionInputError, match="before its recording"):
        projections.project_features(features)
    with pytest.raises(ProjectionInputError, match="analysis_state"):
        projections.project_recording(
            manifest, recording_object_available=True, analysis_state=""
        )

    publication = InMemoryModelPublication()
    model_ref = publication.publish(
        request,
        bundle,
        idempotency_key="published",
    )
    release = publication.release(
        model_ref,
        "current",
        ModelApproval("reviewer", UtcNs(100), "approved"),
        idempotency_key="released",
    )
    with pytest.raises(ProjectionInputError, match="before model publication"):
        projections.project_model(bundle, model_ref, release=release)
    with pytest.raises(ProjectionInputError, match="bundle and published"):
        projections.project_model(
            bundle, replace(model_ref, model_run_id=ModelRunId("mrun_wrong"))
        )

    projections.project_recording(
        manifest, recording_object_available=True, analysis_state="complete"
    )
    projections.project_features(features)
    projections.project_model(bundle, model_ref)
    conflicting_ref = replace(
        model_ref,
        bundle_ref=replace(model_ref.bundle_ref, locator="memory://models/moved"),
    )
    with pytest.raises(ProjectionInputError, match="another published reference"):
        projections.project_model(bundle, conflicting_ref)
    with pytest.raises(ProjectionInputError, match="release and published"):
        projections.project_model(
            bundle, model_ref, release=replace(release, model_ref=conflicting_ref)
        )
    projections.project_model(bundle, model_ref, release=release)
    assert projections.repository().model_snapshot("current").parameter_count == 1


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_application_composes_only_public_contract_and_dashboard_surfaces() -> None:
    forbidden = (
        "leo_flow.analysis",
        "leo_flow.capture",
        "leo_flow.jobs",
        "leo_flow.storage",
        "leo_tracker",
        "requests",
        "socket",
        "subprocess",
    )
    for path in APPLICATION.rglob("*.py"):
        modules = imported_modules(path)
        assert not any(
            module == prefix or module.startswith(prefix + ".")
            for module in modules
            for prefix in forbidden
        ), path


def test_composition_has_no_implicit_latest_or_service_locator() -> None:
    for path in APPLICATION.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        assert "latest_model" not in text
        assert "service_locator" not in text
        assert "current_model" not in text
