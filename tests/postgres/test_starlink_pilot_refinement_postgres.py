from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.starlink_pilot_refinement_postgres import (
    PostgresPilotRefinementWorkRepositoryV0_1,
    PostgresStarlinkPilotRefinementCatalogV0_1,
)
from leo_flow.contracts.core import ArtifactRef, Digest, RecordingId
from leo_flow.contracts.starlink_pilot_refinement import (
    StarlinkPilotRefinementCatalogProjectionV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef


def _object(label: str) -> ObjectRef:
    digest = Digest.sha256(label.encode())
    return ObjectRef(
        digest,
        64,
        "application/octet-stream",
        "test-v0.1",
        f"cas:sha256:{digest.value}",
    )


def _connect_as(postgres_dsn: str, role: str):  # type: ignore[no-untyped-def]
    def connect():  # type: ignore[no-untyped-def]
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        connection.execute(f"SET ROLE {role}")
        return connection

    return connect


def _seed_sources(postgres_dsn: str):  # type: ignore[no-untyped-def]
    recording_id = RecordingId("rec_pilot_refinement_pg")
    data, metadata = _object("data"), _object("metadata")
    prescreen_blob, suite_blob = _object("prescreen"), _object("suite")
    recording = RecordingObjectRef(
        recording_id, data, metadata, Digest.sha256(b"manifest")
    )
    with psycopg.connect(postgres_dsn) as connection:
        for ref in (data, metadata, prescreen_blob, suite_blob):
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
                str(recording_id),
                data.digest.value,
                metadata.digest.value,
                recording.manifest_digest.value,
                "recording:pilot-refinement-pg",
            ),
        )
        connection.execute(
            "INSERT INTO public.recording_starlink_detector_suite VALUES(%s,%s,'sha256',%s,'sha256',%s,'sha256',%s,'candidates',1,8,%s,DEFAULT)",
            (
                "slsuite_" + "1" * 32,
                str(recording_id),
                recording.identity_digest().value,
                Digest.sha256(b"suite-request").value,
                suite_blob.digest.value,
                "suite:pilot-refinement-pg",
            ),
        )
        connection.execute(
            "INSERT INTO public.recording_starlink_pilot_prescreen_v0_1 VALUES(%s,%s,%s,%s,'sha256',%s,1,10,200000,2,%s,DEFAULT)",
            (
                "slps_" + "2" * 32,
                str(recording_id),
                recording.identity_digest().value,
                Digest.sha256(b"prescreen-request").value,
                prescreen_blob.digest.value,
                "prescreen:pilot-refinement-pg",
            ),
        )
    return recording, prescreen_blob, suite_blob


@pytest.mark.integration
def test_pilot_refinement_work_is_fenced_and_catalog_is_replay_safe(
    postgres_dsn: str,
) -> None:
    recording, prescreen_blob, suite_blob = _seed_sources(postgres_dsn)
    analysis_connect = _connect_as(postgres_dsn, "leo_analysis")
    work = PostgresPilotRefinementWorkRepositoryV0_1(
        analysis_connect, token_factory=lambda: "fixed"
    )

    lease = work.claim("worker-a", 120)
    assert lease is not None
    assert lease.recording_ref == recording
    assert lease.source_prescreen_ref.bundle_ref == prescreen_blob
    assert lease.source_suite_ref.bundle_ref == suite_blob
    assert lease.attempt == 1 and lease.lease_generation == 1

    result_blob = _object("refinement-result")
    projection = StarlinkPilotRefinementCatalogProjectionV0_1(
        "slpr_" + "3" * 32,
        recording.recording_id,
        recording.identity_digest(),
        ArtifactRef(
            lease.source_prescreen_ref.analysis_id, prescreen_blob.digest, None
        ),
        ArtifactRef(lease.source_suite_ref.analysis_id, suite_blob.digest, None),
        Digest.sha256(b"refinement-request"),
        1,
        2,
        16,
    )
    catalog = PostgresStarlinkPilotRefinementCatalogV0_1(analysis_connect)
    published = catalog.publish_starlink_pilot_refinement(
        projection,
        result_blob,
        recording,
        idempotency_key="refinement:pilot-refinement-pg",
    )
    assert (
        catalog.publish_starlink_pilot_refinement(
            projection,
            result_blob,
            recording,
            idempotency_key="refinement:pilot-refinement-pg",
        )
        == published
    )
    work.complete(
        lease,
        ArtifactRef(published.analysis_id, published.bundle_ref.digest, None),
    )

    with psycopg.connect(postgres_dsn) as connection:
        state = connection.execute(
            "SELECT state,attempt,result_analysis_id FROM public.starlink_pilot_refinement_work_v0_1"
        ).fetchone()
    assert state == ("succeeded", 1, published.analysis_id)
    assert work.claim("worker-a", 120) is None

    dashboard_catalog = PostgresStarlinkPilotRefinementCatalogV0_1(
        _connect_as(postgres_dsn, "leo_dashboard")
    )
    assert (
        dashboard_catalog.latest_starlink_pilot_refinement(recording.recording_id)
        == published
    )
