from __future__ import annotations

from dataclasses import fields

import pytest

from leo_flow.contracts._validation import thaw_value
from leo_flow.contracts.core import ArtifactRef, Digest, SchemaRef
from leo_flow.contracts.model import ModelAnalysisRequest
from leo_flow.contracts.tracking_input import (
    TRACKING_INPUT_FORMAT_ID,
    TRACKING_INPUT_MEDIA_TYPE,
    TrackingInputSnapshotIdentity,
)
from leo_flow.contracts.tracking_model import TrackingModelAnalysisRequest
from leo_flow.jobs import JobPayload
from leo_flow.services.model_analysis import (
    MODEL_ANALYSIS_JOB_SCHEMA,
    TRACKING_MODEL_ANALYSIS_JOB_SCHEMA,
    ModelAnalysisJobError,
    decode_model_analysis_payload,
    decode_tracking_model_analysis_payload,
    tracking_model_analysis_payload,
)


def _digest(label: str) -> Digest:
    return Digest.sha256(label.encode())


def _identity() -> TrackingInputSnapshotIdentity:
    snapshot_digest = _digest("tracking-snapshot")
    return TrackingInputSnapshotIdentity(
        f"trackinput_{snapshot_digest.value[:32]}",
        snapshot_digest,
        _digest("tracking-membership"),
        _digest("tracking-bundle"),
        4_096,
        TRACKING_INPUT_MEDIA_TYPE,
        TRACKING_INPUT_FORMAT_ID,
    )


def _artifact(name: str, schema: str) -> ArtifactRef:
    return ArtifactRef(name, _digest(name), SchemaRef(schema))


def _request(*, config_id: str = "tracking-config") -> TrackingModelAnalysisRequest:
    return TrackingModelAnalysisRequest(
        SchemaRef(TrackingModelAnalysisRequest.SCHEMA_ID),
        _identity(),
        _artifact(config_id, "org.leo-flow.tracking-model-config"),
        _artifact("tracking-algorithm", "org.leo-flow.model-algorithm"),
    )


def test_tracking_payload_round_trip_is_strict_and_versioned() -> None:
    request = _request()
    payload = tracking_model_analysis_payload(request)

    assert payload.schema == TRACKING_MODEL_ANALYSIS_JOB_SCHEMA
    assert decode_tracking_model_analysis_payload(payload) == request
    with pytest.raises(ModelAnalysisJobError, match="unsupported model-analysis"):
        decode_model_analysis_payload(payload)
    with pytest.raises(ModelAnalysisJobError, match="unsupported tracking"):
        decode_tracking_model_analysis_payload(
            JobPayload.create(MODEL_ANALYSIS_JOB_SCHEMA, thaw_value(payload.value))
        )


@pytest.mark.parametrize("mutation", ["missing", "unknown", "boolean-byte-count"])
def test_tracking_payload_rejects_missing_unknown_and_mistyped_fields(
    mutation: str,
) -> None:
    payload = tracking_model_analysis_payload(_request())
    document = thaw_value(payload.value)
    request = document["request"]
    if mutation == "missing":
        del request["algorithm_ref"]
    elif mutation == "unknown":
        request["unexpected"] = True
    else:
        request["tracking_input_identity"]["bundle"]["byte_count"] = True

    with pytest.raises(ModelAnalysisJobError):
        decode_tracking_model_analysis_payload(
            JobPayload.create(TRACKING_MODEL_ANALYSIS_JOB_SCHEMA, document)
        )


def test_tracking_payload_has_a_hard_canonical_size_bound() -> None:
    with pytest.raises(ModelAnalysisJobError, match="oversized"):
        tracking_model_analysis_payload(_request(config_id="a" * 17_000))


def test_tracking_request_requires_schema_bearing_scientific_artifacts() -> None:
    request = _request()
    with pytest.raises(ValueError, match="require schemas"):
        TrackingModelAnalysisRequest(
            request.schema,
            request.tracking_input_identity,
            ArtifactRef("config", _digest("config")),
            request.algorithm_ref,
        )


def test_descriptive_and_tracking_request_shapes_remain_disjoint() -> None:
    assert {item.name for item in fields(ModelAnalysisRequest)} == {
        "schema",
        "dataset_snapshot_ref",
        "hardware_metadata_snapshot_refs",
        "ephemeris_snapshot_refs",
        "model_config_ref",
        "algorithm_ref",
    }
    assert {item.name for item in fields(TrackingModelAnalysisRequest)} == {
        "schema",
        "tracking_input_identity",
        "model_config_ref",
        "algorithm_ref",
    }
