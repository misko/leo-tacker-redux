from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from leo_flow.analysis.model import (
    ModelConfigurationError,
    ReceiverQualityAggregateConfig,
    ReceiverQualityAggregateModel,
)
from leo_flow.application import (
    DashboardProjectionStore,
    InMemoryModelPublication,
    ModelObjectNotStaged,
    ModelPublicationError,
)
from leo_flow.contracts.capture import ActivityKind
from leo_flow.contracts.core import Digest, UtcNs
from leo_flow.contracts.model import (
    FeatureDatasetSnapshot,
    FeatureDatasetSnapshotRef,
    ModelApproval,
    ModelSnapshotProjection,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.dashboard import DashboardJsonApplication, JsonRequest, JsonResponse

from ._model_fixtures import (
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


def payload(response: JsonResponse) -> Any:
    return json.loads(response.body)


def get(
    application: DashboardJsonApplication,
    path: str,
    query: dict[str, str] | None = None,
) -> JsonResponse:
    return application.handle(JsonRequest("GET", path, query or {}))


def test_frozen_feature_membership_to_explicit_release_and_json_dashboard() -> None:
    dwell = recording_manifest(0, kind=ActivityKind.DWELL, started_utc_ns=1_000)
    scan = recording_manifest(1, kind=ActivityKind.SCAN, started_utc_ns=3_000)
    first = feature_set(dwell, 10.0)
    second = feature_set(scan, 20.0)
    reader = FeatureReader((first, second))
    frozen = dataset((first[0], second[0]))
    hw_ref, hw_snapshot = hardware()
    config = ReceiverQualityAggregateConfig()
    request = model_request(frozen, config, hw_ref)
    fitter = ReceiverQualityAggregateModel(frozen, config, execution_context())

    bundle_before_later_arrival = fitter.fit(
        request,
        reader,
        NoEphemerides(),
        HardwareReader(hw_ref, hw_snapshot),
    )
    assert bundle_before_later_arrival.parameters[0].value == (15.0,)

    # A later catalog arrival is not a member of the already-frozen snapshot.
    # The ModelFitter port has no dataset reader, so the exact immutable dataset
    # is composition-injected and verified against the request reference.
    later_manifest = recording_manifest(
        2, kind=ActivityKind.DWELL, started_utc_ns=5_000
    )
    later = feature_set(later_manifest, 1_000.0)
    reader.add(later)
    call_count = len(reader.calls)
    bundle_after_later_arrival = fitter.fit(
        request,
        reader,
        NoEphemerides(),
        HardwareReader(hw_ref, hw_snapshot),
    )
    assert bundle_after_later_arrival == bundle_before_later_arrival
    assert reader.calls[call_count:] == [first[0], second[0]]

    # The frozen publisher port supplies only ObjectRef plus a projection. The
    # adapter-owned stage models the immutable blob lookup needed to recover
    # model_run_id and validate the complete bundle before publication.
    publication = InMemoryModelPublication()
    bundle_ref = publication.stage(bundle_before_later_arrival)
    model_ref = publication.publish(
        request,
        bundle_ref,
        ModelSnapshotProjection(
            bundle_before_later_arrival.model_snapshot_id,
            len(bundle_before_later_arrival.parameters),
        ),
        idempotency_key="publish-vertical-model",
    )

    projections = DashboardProjectionStore()
    projections.project_recording(
        dwell, recording_object_available=True, analysis_state="complete"
    )
    projections.project_recording(
        scan,
        recording_object_available=False,
        analysis_state="complete",
    )
    projections.project_features(first[1])
    projections.project_features(second[1])
    projections.project_model(bundle_before_later_arrival, model_ref)
    application = projections.json_application()

    recordings = get(
        application,
        "/api/recordings",
        {"start_utc_ns": "0", "stop_utc_ns": "10000"},
    )
    assert recordings.status == 200
    assert [item["recording_id"] for item in payload(recordings)["items"]] == [
        "rec_slice_1",
        "rec_slice_0",
    ]
    activity = payload(
        get(
            application,
            "/api/activity",
            {"start_utc_ns": "0", "stop_utc_ns": "10000"},
        )
    )
    assert {(item["kind"], item["count"]) for item in activity["counts"]} == {
        ("dwell", 1),
        ("scan", 1),
    }
    features = payload(
        get(
            application,
            "/api/recordings/rec_slice_0/features",
            {"selector": "sample-quality"},
        )
    )
    assert features["items"] == [
        {
            "feature_id": "feature_slice_0",
            "method_id": "sample-quality",
            "score": 10,
            "score_semantics": "rms-magnitude-counts",
        }
    ]

    # Blob health/availability is projected metadata, so loss of one recording
    # object does not prevent details, activities, features, or models being read.
    unavailable = payload(get(application, "/api/recordings/rec_slice_1"))
    assert unavailable["recording_object_available"] is False
    assert unavailable["summary"]["analysis_state"] == "complete"
    assert get(application, "/api/storage-health").status == 200

    by_id = get(
        application, f"/api/models/{bundle_before_later_arrival.model_snapshot_id}"
    )
    assert by_id.status == 200
    assert payload(by_id)["release_alias"] is None
    assert get(application, "/api/models/current").status == 404

    release = publication.release(
        model_ref,
        "current",
        ModelApproval(
            approved_by="integration-reviewer",
            approved_utc_ns=UtcNs(30_000),
            rationale="explicit integration fixture approval",
        ),
        idempotency_key="release-current-vertical-model",
    )
    projections.project_model(bundle_before_later_arrival, model_ref, release=release)
    current = get(application, "/api/models/current")
    assert current.status == 200
    assert payload(current)["model_snapshot_id"] == str(
        bundle_before_later_arrival.model_snapshot_id
    )
    assert payload(current)["release_alias"] == "current"


def test_bad_dataset_membership_is_rejected_at_contract_and_fitter_boundaries() -> None:
    manifest = recording_manifest(0, kind=ActivityKind.DWELL, started_utc_ns=1_000)
    entry = feature_set(manifest, 10.0)
    with pytest.raises(ValueError, match="membership_digest"):
        FeatureDatasetSnapshot(
            schema=dataset((entry[0],)).schema,
            snapshot_id=dataset((entry[0],)).snapshot_id,
            ordered_feature_set_refs=(entry[0],),
            selection_spec="bad:test",
            selection_cutoff_utc_ns=UtcNs(10_000),
            membership_digest=digest("wrong-membership"),
        )

    second_manifest = recording_manifest(
        1, kind=ActivityKind.SCAN, started_utc_ns=3_000
    )
    second = feature_set(second_manifest, 20.0)
    frozen = dataset((entry[0], second[0]))
    hw_ref, hw_snapshot = hardware()
    config = ReceiverQualityAggregateConfig()
    valid_request = model_request(frozen, config, hw_ref)
    bad_request = replace(
        valid_request,
        dataset_snapshot_ref=FeatureDatasetSnapshotRef(
            frozen.snapshot_id, digest("wrong-request-membership")
        ),
    )
    reader = FeatureReader((entry, second))
    with pytest.raises(ModelConfigurationError, match="dataset_snapshot_ref"):
        ReceiverQualityAggregateModel(frozen, config, execution_context()).fit(
            bad_request,
            reader,
            NoEphemerides(),
            HardwareReader(hw_ref, hw_snapshot),
        )
    assert not reader.calls


def test_model_publisher_rejects_unavailable_or_wrong_object_digest() -> None:
    dwell = recording_manifest(0, kind=ActivityKind.DWELL, started_utc_ns=1_000)
    scan = recording_manifest(1, kind=ActivityKind.SCAN, started_utc_ns=3_000)
    first = feature_set(dwell, 10.0)
    second = feature_set(scan, 20.0)
    frozen = dataset((first[0], second[0]))
    hw_ref, hw_snapshot = hardware()
    config = ReceiverQualityAggregateConfig()
    request = model_request(frozen, config, hw_ref)
    bundle = ReceiverQualityAggregateModel(frozen, config, execution_context()).fit(
        request,
        FeatureReader((first, second)),
        NoEphemerides(),
        HardwareReader(hw_ref, hw_snapshot),
    )
    projection = ModelSnapshotProjection(bundle.model_snapshot_id, 1)
    publication = InMemoryModelPublication()
    unavailable = ObjectRef(
        digest=Digest.sha256(b"unavailable"),
        byte_count=12,
        media_type="application/json",
        format_id="model-snapshot-bundle-v0.1",
        locator="memory://models/unavailable",
    )
    with pytest.raises(ModelObjectNotStaged, match="not staged"):
        publication.publish(
            request, unavailable, projection, idempotency_key="unavailable"
        )

    staged = publication.stage(bundle)
    wrong_metadata = replace(staged, byte_count=staged.byte_count + 1)
    with pytest.raises(ModelPublicationError, match="metadata"):
        publication.publish(
            request, wrong_metadata, projection, idempotency_key="wrong-metadata"
        )


def test_existing_capture_analysis_slice_is_preserved_as_an_independent_test() -> None:
    # The capture -> publication -> RecordingView -> one-recording analyzer test
    # remains in its original module. This guard prevents composition work from
    # replacing that public boundary with application-layer private shortcuts.
    source = Path(__file__).with_name("test_recording_vertical_slice.py")
    text = source.read_text(encoding="utf-8")
    assert "PlanCaptureEngine" in text
    assert "SigMFRecordingObjectReader" in text
    assert "QualityPsdAnalyzer" in text
    assert "leo_flow.application" not in text
