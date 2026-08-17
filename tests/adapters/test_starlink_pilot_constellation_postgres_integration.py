from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from leo_flow.adapters.starlink_pilot_constellation_postgres import (
    PostgresStarlinkPilotConstellationCatalogV0_1,
)
from leo_flow.adapters.starlink_suite_postgres import PostgresStarlinkSuiteCatalogV0_2
from leo_flow.analysis.recording.starlink_pilot_constellation_persistence import (
    StarlinkPilotConstellationConflictError,
)
from leo_flow.analysis.recording.starlink_suite_persistence import (
    StarlinkSuiteCatalogProjectionV0_2,
)
from leo_flow.contracts.core import ArtifactRef, Digest, RecordingId, SchemaRef
from leo_flow.contracts.starlink_detector_suite import V0_2
from leo_flow.contracts.starlink_pilot_constellation_pipeline import (
    StarlinkPilotConstellationCatalogProjectionV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
from leo_flow.storage.postgres_migrations import apply_migrations


@pytest.fixture(scope="module")
def constellation_postgres_dsn() -> Iterator[str]:
    admin_dsn = os.environ.get(
        "LEO_CONSTELLATION_TEST_ADMIN_DSN", "postgresql://127.0.0.1:55432/postgres"
    )
    database = f"leo_qam_{uuid.uuid4().hex}"
    try:
        admin = psycopg.connect(admin_dsn, autocommit=True, connect_timeout=3)
    except psycopg.Error as error:
        pytest.skip(f"disposable PostgreSQL 16 is unavailable: {error}")
    with admin:
        assert (
            int(admin.execute("SHOW server_version_num").fetchone()[0]) // 10_000 == 16
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    options = conninfo_to_dict(admin_dsn)
    options["dbname"] = database
    dsn = make_conninfo(**options)
    try:
        with psycopg.connect(dsn) as connection:
            apply_migrations(connection, Path("migrations"))
        yield dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as cleanup:
            cleanup.execute(
                "SELECT pg_catalog.pg_terminate_backend(pid) FROM pg_catalog.pg_stat_activity WHERE datname=%s",
                (database,),
            )
            cleanup.execute(
                sql.SQL("DROP DATABASE {}").format(sql.Identifier(database))
            )


def _connect(
    dsn: str, role: str
) -> Callable[[], psycopg.Connection[dict[str, object]]]:
    def connect() -> psycopg.Connection[dict[str, object]]:
        connection: psycopg.Connection[dict[str, object]] = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        return connection

    return connect


def _object(label: bytes, locator: str) -> ObjectRef:
    return ObjectRef(
        Digest.sha256(label), 64, "application/json", "qam-pg-test-v0.1", locator
    )


def test_exact_source_closure_replay_conflict_and_acl(
    constellation_postgres_dsn: str,
) -> None:
    dsn = constellation_postgres_dsn
    recording = RecordingObjectRef(
        RecordingId("rec_qam_postgres"),
        _object(b"data", "cas:data"),
        _object(b"metadata", "cas:metadata"),
        Digest.sha256(b"manifest"),
    )
    PostgresRecordingCatalog(_connect(dsn, "leo_capture")).publish(
        recording, idempotency_key="qam:recording"
    )
    source_request = Digest.sha256(b"source-request")
    source_blob = _object(b"suite", "cas:suite")
    source_projection = StarlinkSuiteCatalogProjectionV0_2(
        "slsuite_" + "3" * 32,
        str(recording.recording_id),
        recording.identity_digest(),
        source_request,
        "candidates",
        1,
        8,
    )
    PostgresStarlinkSuiteCatalogV0_2(
        _connect(dsn, "leo_analysis")
    ).publish_starlink_suite(
        source_projection, source_blob, recording, idempotency_key="qam:suite"
    )
    source_ref = ArtifactRef(
        source_projection.analysis_id,
        source_blob.digest,
        SchemaRef("org.leo-flow.starlink-detector-suite-recording-bundle", V0_2),
    )
    projection = StarlinkPilotConstellationCatalogProjectionV0_1(
        "slqamrec_" + "4" * 32,
        recording.recording_id,
        recording.identity_digest(),
        source_ref,
        source_request,
        Digest.sha256(b"qam-request"),
        1,
        2_400,
    )
    blob = _object(b"qam-bundle", "cas:qam")
    catalog = PostgresStarlinkPilotConstellationCatalogV0_1(
        _connect(dsn, "leo_analysis")
    )
    first = catalog.publish_starlink_pilot_constellation(
        projection, blob, recording, idempotency_key="qam:publish"
    )
    assert (
        catalog.publish_starlink_pilot_constellation(
            projection, blob, recording, idempotency_key="qam:publish"
        )
        == first
    )
    assert catalog.get_starlink_pilot_constellation(first).projection == projection
    assert catalog.latest_starlink_pilot_constellation(recording.recording_id) == first
    with pytest.raises(StarlinkPilotConstellationConflictError):
        catalog.publish_starlink_pilot_constellation(
            projection,
            _object(b"other", "cas:other"),
            recording,
            idempotency_key="qam:publish",
        )
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT has_function_privilege('leo_dashboard','read_latest_recording_starlink_pilot_constellation(text)','EXECUTE')"
            ).fetchone()[0]
            is True
        )
        assert (
            connection.execute(
                "SELECT has_function_privilege('leo_dashboard','read_recording_starlink_pilot_constellation(text,text,text,text)','EXECUTE')"
            ).fetchone()[0]
            is True
        )
        columns = [
            item.name
            for item in connection.execute(
                "SELECT * FROM public.read_latest_recording_starlink_pilot_constellation(%s)",
                (str(recording.recording_id),),
            ).description
        ]
        assert "bundle_locator" not in columns
