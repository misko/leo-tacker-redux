# ruff: noqa: F401,F811 -- importing the module-scoped PostgreSQL fixture registers it.
from __future__ import annotations

from dataclasses import replace

import psycopg
import pytest
from psycopg import sql

from leo_flow.adapters.starlink_full_dwell_postgres import (
    PostgresStarlinkFullDwellCatalogV0_1,
)
from leo_flow.adapters.starlink_suite_postgres import PostgresStarlinkSuiteCatalogV0_2
from leo_flow.analysis.recording.starlink_full_dwell_response_codec import (
    encode_starlink_full_dwell_response,
)
from leo_flow.analysis.recording.starlink_full_dwell_response_persistence import (
    starlink_full_dwell_projection_v0_1,
)
from leo_flow.analysis.recording.starlink_suite_persistence import (
    StarlinkSuiteCatalogProjectionV0_2,
)
from leo_flow.contracts.core import ArtifactRef, Digest, SchemaRef
from leo_flow.contracts.starlink_detector_suite import V0_2
from leo_flow.contracts.storage import ObjectRef
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
from tests.adapters.test_starlink_pilot_constellation_postgres_integration import (
    constellation_postgres_dsn,
)
from tests.recording_analysis.test_starlink_full_dwell_response import full_dwell_result


def _connect(dsn: str, role: str):
    def connect():
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        return connection

    return connect


def _object(payload: bytes, format_id: str, locator: str) -> ObjectRef:
    return ObjectRef(
        Digest.sha256(payload), len(payload), "application/json", format_id, locator
    )


@pytest.mark.integration
def test_full_dwell_pg16_exact_replay_points_and_live_reference(
    constellation_postgres_dsn: str, full_dwell_result
) -> None:
    _view, base_request, base_bundle = full_dwell_result
    recording = base_request.recording_object_ref
    PostgresRecordingCatalog(
        _connect(constellation_postgres_dsn, "leo_capture")
    ).publish(recording, idempotency_key="full-dwell:recording")
    source_request = Digest.sha256(b"full-dwell-source-request")
    source_blob = _object(b"full-dwell-suite", "suite-pg-test-v0.2", "cas:fd-suite")
    source_projection = StarlinkSuiteCatalogProjectionV0_2(
        "slsuite_" + "8" * 32,
        str(recording.recording_id),
        recording.identity_digest(),
        source_request,
        "candidates",
        1,
        8,
    )
    PostgresStarlinkSuiteCatalogV0_2(
        _connect(constellation_postgres_dsn, "leo_analysis")
    ).publish_starlink_suite(
        source_projection, source_blob, recording, idempotency_key="full-dwell:suite"
    )
    source_ref = ArtifactRef(
        source_projection.analysis_id,
        source_blob.digest,
        SchemaRef("org.leo-flow.starlink-detector-suite-recording-bundle", V0_2),
    )
    request = replace(
        base_request,
        source_suite_ref=source_ref,
        source_suite_request_digest=source_request,
    )
    bundle = replace(
        base_bundle,
        analysis_id="slfd_" + "9" * 32,
        source_suite_ref=source_ref,
        source_suite_request_digest=source_request,
        request_digest=request.digest,
    )
    payload = encode_starlink_full_dwell_response(bundle)
    bundle_ref = _object(payload, "starlink-full-dwell-response-v0.1", "cas:fd-bundle")
    projection = starlink_full_dwell_projection_v0_1(request, bundle)
    catalog = PostgresStarlinkFullDwellCatalogV0_1(
        _connect(constellation_postgres_dsn, "leo_analysis")
    )
    first = catalog.publish_starlink_full_dwell(
        projection,
        bundle_ref,
        recording,
        idempotency_key="full-dwell:publish",
        bundle=bundle,
    )
    assert (
        catalog.publish_starlink_full_dwell(
            projection,
            bundle_ref,
            recording,
            idempotency_key="full-dwell:publish",
            bundle=bundle,
        )
        == first
    )
    assert catalog.get_starlink_full_dwell(first).projection == projection
    assert catalog.latest_starlink_full_dwell(recording.recording_id) == first
    with psycopg.connect(constellation_postgres_dsn) as connection:
        point_count = connection.execute(
            "SELECT count(*) FROM recording_starlink_full_dwell_point_v0_1 WHERE analysis_id=%s",
            (bundle.analysis_id,),
        ).fetchone()[0]
        assert point_count == sum(len(stream.points) for stream in bundle.streams)
        assert (
            connection.execute(
                "SELECT reference_kind FROM object_blob_live_reference WHERE digest_value=%s",
                (bundle_ref.digest.value,),
            ).fetchone()[0]
            == "recording_starlink_full_dwell_v0_1.bundle"
        )


@pytest.mark.integration
def test_full_dwell_catalog_pg16_live_closure_and_least_privilege(
    constellation_postgres_dsn: str,
) -> None:
    with psycopg.connect(constellation_postgres_dsn) as connection:
        assert (
            int(connection.execute("SHOW server_version_num").fetchone()[0]) // 10_000
            == 16
        )
        table_acl = connection.execute(
            """SELECT has_table_privilege('leo_dashboard','recording_starlink_full_dwell_v0_1','SELECT'),
                      has_table_privilege('leo_analysis','recording_starlink_full_dwell_v0_1','INSERT'),
                      has_table_privilege('leo_capture','recording_starlink_full_dwell_v0_1','SELECT')"""
        ).fetchone()
        assert table_acl == (False, False, False)
        routine_acl = connection.execute(
            """SELECT has_function_privilege('leo_dashboard','read_recording_starlink_full_dwell_v0_1(text,text[],text[],text[],text[],integer)','EXECUTE'),
                      has_function_privilege('leo_dashboard','publish_recording_starlink_full_dwell_v0_1(jsonb,jsonb)','EXECUTE'),
                      has_function_privilege('leo_analysis','publish_recording_starlink_full_dwell_v0_1(jsonb,jsonb)','EXECUTE'),
                      has_function_privilege('leo_capture','publish_recording_starlink_full_dwell_v0_1(jsonb,jsonb)','EXECUTE')"""
        ).fetchone()
        assert routine_acl == (True, False, True, False)
        owner, security, config = connection.execute(
            """SELECT pg_catalog.pg_get_userbyid(p.proowner),p.prosecdef,p.proconfig
                 FROM pg_catalog.pg_proc p
                WHERE p.oid='read_recording_starlink_full_dwell_v0_1(text,text[],text[],text[],text[],integer)'::regprocedure"""
        ).fetchone()
        assert (owner, security, config) == (
            "leo_routine_owner",
            True,
            ["search_path=pg_catalog, pg_temp"],
        )
        reference = connection.execute(
            "SELECT reference_kind FROM object_blob_live_reference WHERE reference_kind='recording_starlink_full_dwell_v0_1.bundle' LIMIT 1"
        ).fetchone()
        assert reference is None or reference == (
            "recording_starlink_full_dwell_v0_1.bundle",
        )


@pytest.mark.integration
def test_full_dwell_dashboard_routine_rejects_unbounded_reads(
    constellation_postgres_dsn: str,
) -> None:
    with psycopg.connect(constellation_postgres_dsn) as connection:
        connection.execute("SET ROLE leo_dashboard")
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            connection.execute(
                "SELECT * FROM read_recording_starlink_full_dwell_v0_1(%s,%s,%s,%s,%s,%s)",
                ("rec_missing", [], [], [], [], 4097),
            ).fetchall()
