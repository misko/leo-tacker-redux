from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from leo_flow.adapters.starlink_suite_postgres import (
    PostgresStarlinkSuiteCatalogV0_2,
)
from leo_flow.adapters.starlink_surrogate_null_postgres import (
    PostgresStarlinkSurrogateNullCatalogV0_1,
)
from leo_flow.analysis.recording.starlink_suite_persistence import (
    StarlinkSuiteCatalogProjectionV0_2,
)
from leo_flow.analysis.recording.starlink_surrogate_null_persistence import (
    StarlinkSurrogateNullConflictError,
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
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
from leo_flow.storage.postgres_migrations import apply_migrations

_PUBLISH_SIGNATURE = (
    "publish_recording_starlink_surrogate_null("
    "text,text,text,text,text,text,text,text,integer,integer,text,text,text,text,"
    "text,text,text,integer,integer,integer,text)"
)


@pytest.fixture(scope="module")
def surrogate_null_postgres_dsn() -> Iterator[str]:
    admin_dsn = os.environ.get(
        "LEO_SURROGATE_NULL_TEST_ADMIN_DSN",
        "postgresql://127.0.0.1:55432/postgres",
    )
    database = f"leo_snull_{uuid.uuid4().hex}"
    try:
        admin = psycopg.connect(admin_dsn, autocommit=True, connect_timeout=3)
    except psycopg.Error as error:
        pytest.skip(f"disposable PostgreSQL 16 is unavailable: {error}")
    with admin:
        version = int(admin.execute("SHOW server_version_num").fetchone()[0])
        if version // 10_000 != 16:
            pytest.fail("surrogate-null integration requires PostgreSQL 16")
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
                "SELECT pg_catalog.pg_terminate_backend(pid) "
                "FROM pg_catalog.pg_stat_activity WHERE datname=%s",
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


def _object(label: bytes, locator: str, *, byte_count: int = 16) -> ObjectRef:
    return ObjectRef(
        Digest.sha256(label),
        byte_count,
        "application/octet-stream",
        "surrogate-null-postgres-test-v1",
        locator,
    )


def _seed(
    dsn: str,
) -> tuple[
    RecordingObjectRef,
    StarlinkSuiteCatalogProjectionV0_2,
    ObjectRef,
    StarlinkSurrogateNullCatalogProjectionV0_1,
    ObjectRef,
]:
    recording = RecordingObjectRef(
        RecordingId("rec_surrogate_null_postgres"),
        _object(b"recording-data", "cas/test/recording-data"),
        _object(b"recording-metadata", "cas/test/recording-metadata"),
        Digest.sha256(b"manifest"),
    )
    PostgresRecordingCatalog(_connect(dsn, "leo_capture")).publish(
        recording, idempotency_key="recording:surrogate-null"
    )
    suite_bundle = _object(b"source-suite", "cas/test/source-suite", byte_count=64)
    suite = StarlinkSuiteCatalogProjectionV0_2(
        "slsuite_" + "2" * 32,
        str(recording.recording_id),
        recording.identity_digest(),
        Digest.sha256(b"source-suite-request"),
        "candidates",
        1,
        8,
    )
    PostgresStarlinkSuiteCatalogV0_2(
        _connect(dsn, "leo_analysis")
    ).publish_starlink_suite(
        suite,
        suite_bundle,
        recording,
        idempotency_key="suite:surrogate-null",
    )
    surrogate_bundle = _object(
        b"surrogate-null", "cas/test/surrogate-null", byte_count=128
    )
    projection = StarlinkSurrogateNullCatalogProjectionV0_1(
        "slsnullrec_" + "3" * 32,
        recording.recording_id,
        recording.identity_digest(),
        ArtifactRef(
            suite.analysis_id,
            suite_bundle.digest,
            SchemaRef(
                "org.leo-flow.starlink-detector-suite-recording-bundle",
                SchemaVersion(0, 2),
            ),
        ),
        suite.request_digest,
        Digest.sha256(b"surrogate-null-request"),
        StarlinkSurrogateNullRecordingState.CANDIDATES,
        1,
        8,
        32,
    )
    return recording, suite, suite_bundle, projection, surrogate_bundle


@pytest.mark.integration
def test_surrogate_null_catalog_exact_replay_conflict_and_identity_closure(
    surrogate_null_postgres_dsn: str,
) -> None:
    dsn = surrogate_null_postgres_dsn
    recording, _, _, projection, bundle = _seed(dsn)
    catalog = PostgresStarlinkSurrogateNullCatalogV0_1(_connect(dsn, "leo_analysis"))

    first = catalog.publish_starlink_surrogate_null(
        projection, bundle, recording, idempotency_key="surrogate:null:1"
    )
    replay = catalog.publish_starlink_surrogate_null(
        projection, bundle, recording, idempotency_key="surrogate:null:1"
    )

    assert replay == first
    assert catalog.get_starlink_surrogate_null(first).projection == projection
    assert catalog.latest_starlink_surrogate_null(recording.recording_id) == first
    with pytest.raises(StarlinkSurrogateNullConflictError):
        catalog.publish_starlink_surrogate_null(
            replace(projection, request_digest=Digest.sha256(b"drift")),
            bundle,
            recording,
            idempotency_key="surrogate:null:1",
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        catalog.publish_starlink_surrogate_null(
            replace(
                projection,
                analysis_id="slsnullrec_" + "4" * 32,
                source_suite_request_digest=Digest.sha256(b"source-request-drift"),
            ),
            bundle,
            recording,
            idempotency_key="surrogate:null:source-request-drift",
        )
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        catalog.publish_starlink_surrogate_null(
            replace(
                projection,
                analysis_id="slsnullrec_" + "5" * 32,
                source_suite_ref=replace(
                    projection.source_suite_ref,
                    schema=SchemaRef(
                        "org.leo-flow.starlink-detector-suite-recording-bundle",
                        SchemaVersion(0, 1),
                    ),
                ),
            ),
            bundle,
            recording,
            idempotency_key="surrogate:null:source-schema-drift",
        )
    with (
        pytest.raises(psycopg.errors.ForeignKeyViolation),
        psycopg.connect(dsn) as connection,
    ):
        connection.execute("SET ROLE leo_analysis")
        connection.execute(
            "SELECT public.publish_recording_starlink_surrogate_null("
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                "slsnullrec_" + "4" * 32,
                str(recording.recording_id),
                projection.input_recording_digest.algorithm.value,
                projection.input_recording_digest.value,
                "slsuite_" + "9" * 32,
                projection.source_suite_ref.digest.algorithm.value,
                projection.source_suite_ref.digest.value,
                projection.source_suite_ref.schema.schema_id,
                0,
                2,
                projection.source_suite_request_digest.algorithm.value,
                projection.source_suite_request_digest.value,
                projection.request_digest.algorithm.value,
                projection.request_digest.value,
                bundle.digest.algorithm.value,
                bundle.digest.value,
                projection.state.value,
                projection.stream_count,
                projection.method_count,
                projection.surrogate_score_count,
                "surrogate:null:missing-source",
            ),
        )

    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT reference_kind,owner_id FROM object_blob_live_reference "
            "WHERE digest_algorithm=%s AND digest_value=%s",
            (bundle.digest.algorithm.value, bundle.digest.value),
        ).fetchall() == [
            ("recording_starlink_surrogate_null.bundle", projection.analysis_id)
        ]


@pytest.mark.integration
def test_surrogate_null_catalog_security_and_bounded_dashboard_reads(
    surrogate_null_postgres_dsn: str,
) -> None:
    dsn = surrogate_null_postgres_dsn
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT pg_catalog.pg_get_userbyid(relowner) "
            "FROM pg_catalog.pg_class "
            "WHERE oid='public.recording_starlink_surrogate_null'::regclass"
        ).fetchone() == ("leo_routine_owner",)
        routines = connection.execute(
            """SELECT p.oid::regprocedure::text,
                      pg_catalog.pg_get_userbyid(p.proowner),
                      p.prosecdef,p.proconfig
                 FROM pg_catalog.pg_proc AS p
                WHERE p.proname IN (
                    'publish_recording_starlink_surrogate_null',
                    'read_recording_starlink_surrogate_null',
                    'read_latest_recording_starlink_surrogate_null')
                ORDER BY p.proname"""
        ).fetchall()
        assert len(routines) == 3
        assert all(
            row[1:] == ("leo_routine_owner", True, ["search_path=pg_catalog, pg_temp"])
            for row in routines
        )
        for role in ("leo_capture", "leo_maintenance"):
            assert connection.execute(
                "SELECT has_table_privilege(%s,'recording_starlink_surrogate_null','SELECT'),"
                "has_function_privilege(%s,%s,'EXECUTE')",
                (role, role, _PUBLISH_SIGNATURE),
            ).fetchone() == (False, False)
        assert connection.execute(
            "SELECT has_table_privilege('leo_analysis','recording_starlink_surrogate_null','SELECT'),"
            "has_function_privilege('leo_analysis',%s,'EXECUTE'),"
            "has_function_privilege('leo_dashboard',%s,'EXECUTE'),"
            "has_function_privilege('leo_dashboard','read_latest_recording_starlink_surrogate_null(text)','EXECUTE'),"
            "has_function_privilege('leo_dashboard','read_recording_starlink_surrogate_null(text,text,text,text)','EXECUTE')",
            (_PUBLISH_SIGNATURE, _PUBLISH_SIGNATURE),
        ).fetchone() == (False, True, False, True, True)

        connection.execute("SET ROLE leo_dashboard")
        description = connection.execute(
            "SELECT * FROM public.read_latest_recording_starlink_surrogate_null(%s)",
            ("rec_absent",),
        ).description
        assert [column.name for column in description] == [
            "analysis_id",
            "recording_id",
            "result_state",
            "stream_count",
            "method_count",
            "surrogate_score_count",
            "bundle_digest_algorithm",
            "bundle_digest_value",
            "published_at_utc",
        ]
        assert "locator" not in {column.name for column in description}
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                "SELECT * FROM public.recording_starlink_surrogate_null"
            ).fetchall()
