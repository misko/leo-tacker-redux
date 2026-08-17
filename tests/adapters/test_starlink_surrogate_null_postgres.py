from __future__ import annotations

from leo_flow.adapters.starlink_surrogate_null_postgres import (
    _cataloged,
    _parameters,
)
from leo_flow.analysis.recording.starlink_surrogate_null_persistence import (
    CatalogedStarlinkSurrogateNullV0_1,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    RecordingId,
    SchemaRef,
    SchemaVersion,
)
from leo_flow.contracts.starlink_surrogate_null_pipeline import (
    StarlinkSurrogateNullCatalogProjectionV0_1,
    StarlinkSurrogateNullRecordingState,
)
from leo_flow.contracts.storage import ObjectRef


def _projection() -> StarlinkSurrogateNullCatalogProjectionV0_1:
    return StarlinkSurrogateNullCatalogProjectionV0_1(
        "slsnullrec_" + "1" * 32,
        RecordingId("rec_surrogate_null_postgres"),
        Digest.sha256(b"recording"),
        ArtifactRef(
            "slsuite_" + "2" * 32,
            Digest.sha256(b"source-suite"),
            SchemaRef(
                "org.leo-flow.starlink-detector-suite-recording-bundle",
                SchemaVersion(0, 2),
            ),
        ),
        Digest.sha256(b"source-request"),
        Digest.sha256(b"surrogate-request"),
        StarlinkSurrogateNullRecordingState.CANDIDATES,
        1,
        8,
        32,
    )


def test_parameter_mapping_closes_source_recording_and_suite_identity() -> None:
    projection = _projection()
    bundle = ObjectRef(
        Digest.sha256(b"surrogate-bundle"),
        123,
        "application/json",
        "starlink-surrogate-null-recording-bundle-v0.1",
        "cas/sha256/surrogate",
    )

    values = _parameters(projection, bundle, idempotency_key="surrogate:null:1")

    assert values["input_digest"] == projection.input_recording_digest.value
    assert values["source_analysis_id"] == projection.source_suite_ref.artifact_id
    assert values["source_bundle_digest"] == projection.source_suite_ref.digest.value
    assert values["source_schema_major"] == 0
    assert values["source_schema_minor"] == 2
    assert (
        values["source_request_digest"] == projection.source_suite_request_digest.value
    )
    assert values["request_digest"] == projection.request_digest.value
    assert values["bundle_digest"] == bundle.digest.value


def test_catalog_row_round_trip_preserves_exact_projection_and_object() -> None:
    projection = _projection()
    bundle = ObjectRef(
        Digest.sha256(b"surrogate-bundle"),
        123,
        "application/json",
        "starlink-surrogate-null-recording-bundle-v0.1",
        "cas/sha256/surrogate",
    )
    values = _parameters(projection, bundle, idempotency_key="surrogate:null:1")
    row: dict[str, object] = {
        "analysis_id": values["analysis_id"],
        "recording_id": values["recording_id"],
        "input_recording_digest_algorithm": values["input_algorithm"],
        "input_recording_digest_value": values["input_digest"],
        "source_suite_analysis_id": values["source_analysis_id"],
        "source_suite_bundle_digest_algorithm": values["source_bundle_algorithm"],
        "source_suite_bundle_digest_value": values["source_bundle_digest"],
        "source_suite_schema_id": values["source_schema_id"],
        "source_suite_schema_major": values["source_schema_major"],
        "source_suite_schema_minor": values["source_schema_minor"],
        "source_suite_request_digest_algorithm": values["source_request_algorithm"],
        "source_suite_request_digest_value": values["source_request_digest"],
        "request_digest_algorithm": values["request_algorithm"],
        "request_digest_value": values["request_digest"],
        "result_state": values["state"],
        "stream_count": values["stream_count"],
        "method_count": values["method_count"],
        "surrogate_score_count": values["score_count"],
        "bundle_digest_algorithm": values["bundle_algorithm"],
        "bundle_digest_value": values["bundle_digest"],
        "bundle_byte_count": bundle.byte_count,
        "bundle_media_type": bundle.media_type,
        "bundle_format_id": bundle.format_id,
        "bundle_locator": bundle.locator,
    }

    assert _cataloged(row) == CatalogedStarlinkSurrogateNullV0_1(projection, bundle)
