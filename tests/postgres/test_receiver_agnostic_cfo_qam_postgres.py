from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.adapters.starlink_receiver_agnostic_cfo_qam_postgres import (
    PostgresReceiverAgnosticCfoQamCatalogV0_6,
)
from leo_flow.analysis.recording.starlink_receiver_agnostic_cfo_product_persistence import (
    receiver_agnostic_cfo_qam_projection_v0_6,
)
from leo_flow.contracts.core import Digest
from leo_flow.contracts.storage import ObjectRef
from tests.recording_analysis.receiver_agnostic_cfo_product_fixtures import product_pair


def _connect_as(postgres_dsn: str, role: str):  # type: ignore[no-untyped-def]
    def connect():  # type: ignore[no-untyped-def]
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        connection.execute(f"SET ROLE {role}")
        return connection

    return connect


def _seed(postgres_dsn: str, label: str):  # type: ignore[no-untyped-def]
    request, bundle = product_pair(f"rec_cfo_qam_pg_{label}")
    recording = request.recording_object_ref
    with psycopg.connect(postgres_dsn) as connection:
        for ref in (recording.data_object, recording.metadata_object):
            connection.execute(
                "SELECT public.register_live_object_blob(%s,%s,%s,%s,%s,%s)",
                (
                    ref.digest.algorithm.value,
                    ref.digest.value,
                    ref.byte_count,
                    ref.media_type,
                    ref.format_id,
                    ref.locator,
                ),
            )
        connection.execute(
            "INSERT INTO public.recording(recording_id,data_digest_value,metadata_digest_value,manifest_digest_value,idempotency_key,state) VALUES(%s,%s,%s,%s,%s,'published')",
            (
                str(recording.recording_id),
                recording.data_object.digest.value,
                recording.metadata_object.digest.value,
                recording.manifest_digest.value,
                f"recording:cfo-qam-pg:{label}",
            ),
        )
    digest = Digest.sha256(b"cfo-qam-bundle")
    blob = ObjectRef(
        digest,
        64,
        "application/json",
        "receiver-agnostic-cfo-qam-v0.6",
        f"cas:sha256:{digest.value}",
    )
    return request, bundle, blob


@pytest.mark.integration
def test_analysis_publishes_dashboard_reads_and_capture_has_no_capability(
    postgres_dsn: str,
) -> None:
    request, bundle, blob = _seed(postgres_dsn, "happy")
    projection = receiver_agnostic_cfo_qam_projection_v0_6(request, bundle)
    analysis = PostgresReceiverAgnosticCfoQamCatalogV0_6(
        _connect_as(postgres_dsn, "leo_analysis")
    )
    published = analysis.publish_receiver_agnostic_cfo_qam(
        projection, blob, request.recording_object_ref, idempotency_key="cfo-qam:pg"
    )
    assert (
        analysis.publish_receiver_agnostic_cfo_qam(
            projection, blob, request.recording_object_ref, idempotency_key="cfo-qam:pg"
        )
        == published
    )
    dashboard = PostgresReceiverAgnosticCfoQamCatalogV0_6(
        _connect_as(postgres_dsn, "leo_dashboard")
    )
    assert dashboard.latest_receiver_agnostic_cfo_qam(bundle.recording_id) == published
    assert dashboard.get_receiver_agnostic_cfo_qam(published).ref == published
    capture = PostgresReceiverAgnosticCfoQamCatalogV0_6(
        _connect_as(postgres_dsn, "leo_capture")
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        capture.latest_receiver_agnostic_cfo_qam(bundle.recording_id)


@pytest.mark.integration
@pytest.mark.parametrize("object_name", ("data_object", "metadata_object"))
def test_publication_rejects_recording_when_either_source_object_is_not_live(
    postgres_dsn: str,
    object_name: str,
) -> None:
    request, bundle, blob = _seed(postgres_dsn, f"non_live_{object_name}")
    projection = receiver_agnostic_cfo_qam_projection_v0_6(request, bundle)
    with psycopg.connect(postgres_dsn) as connection:
        source_object = getattr(request.recording_object_ref, object_name)
        connection.execute(
            "SELECT public.register_live_object_blob(%s,%s,%s,%s,%s,%s)",
            (
                blob.digest.algorithm.value,
                blob.digest.value,
                blob.byte_count,
                blob.media_type,
                blob.format_id,
                blob.locator,
            ),
        )
        connection.execute(
            "UPDATE public.object_blob SET lifecycle_state='gc_delete_failed' "
            "WHERE digest_algorithm=%s AND digest_value=%s",
            (source_object.digest.algorithm.value, source_object.digest.value),
        )
        connection.execute("SET ROLE leo_analysis")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                "SELECT public.publish_recording_receiver_agnostic_cfo_qam_v0_6(%s)",
                (
                    Jsonb(
                        {
                            "analysis_id": projection.analysis_id,
                            "recording_id": str(projection.recording_id),
                            "recording_identity_digest_value": projection.recording_identity_digest.value,
                            "request_digest_value": projection.request_digest.value,
                            "bundle_digest_value": blob.digest.value,
                            "stream_count": projection.stream_count,
                            "window_count": projection.window_count,
                            "pattern_evidence_count": projection.pattern_evidence_count,
                            "unique_cell_count": projection.unique_cell_count,
                            "pattern_evaluation_count": projection.pattern_evaluation_count,
                            "candidates_only": True,
                            "idempotency_key": "cfo-qam:non-live-recording",
                        }
                    ),
                ),
            )
