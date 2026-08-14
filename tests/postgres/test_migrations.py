from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from leo_flow.storage.postgres_migrations import MigrationError, apply_migrations


@pytest.mark.integration
def test_migrations_are_idempotent_and_recorded(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        assert apply_migrations(connection, Path("migrations")) == ()
        rows = connection.execute(
            "SELECT name FROM schema_migration ORDER BY name"
        ).fetchall()
    assert rows == [
        ("0001_first_slice.sql",),
        ("0002_capability_roles.sql",),
        ("0003_ephemeris_catalog.sql",),
        ("0004_dashboard_projections.sql",),
        ("0005_dataset_snapshots.sql",),
        ("0006_dashboard_projection_identity.sql",),
        ("0007_feature_set_catalog.sql",),
        ("0008_model_snapshot_catalog.sql",),
    ]


@pytest.mark.integration
def test_applied_migration_cannot_be_silently_rewritten(
    postgres_dsn: str, tmp_path: Path
) -> None:
    changed = tmp_path / "0001_first_slice.sql"
    changed.write_text("BEGIN;\nSELECT 1;\nCOMMIT;\n")
    with (
        psycopg.connect(postgres_dsn) as connection,
        pytest.raises(MigrationError, match="changed"),
    ):
        apply_migrations(connection, tmp_path)


@pytest.mark.integration
def test_capability_roles_are_narrow(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        capture_insert, capture_job, dashboard_select, dashboard_mutate = (
            connection.execute(
                """
            SELECT has_table_privilege('leo_capture', 'recording', 'INSERT'),
                   has_table_privilege('leo_capture', 'job', 'UPDATE'),
                   has_table_privilege('leo_dashboard', 'recording', 'SELECT'),
                   has_table_privilege('leo_dashboard', 'recording', 'UPDATE')
            """
            ).fetchone()
        )
    assert capture_insert
    assert not capture_job
    assert dashboard_select
    assert not dashboard_mutate


@pytest.mark.integration
def test_ephemeris_capabilities_are_narrow(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        analysis_insert, analysis_update, dashboard_read, capture_read = (
            connection.execute(
                """
                SELECT has_table_privilege(
                           'leo_analysis', 'ephemeris_snapshot', 'INSERT'),
                       has_table_privilege(
                           'leo_analysis', 'ephemeris_snapshot', 'UPDATE'),
                       has_table_privilege(
                           'leo_dashboard', 'ephemeris_snapshot', 'SELECT'),
                       has_table_privilege(
                           'leo_capture', 'ephemeris_snapshot', 'SELECT')
                """
            ).fetchone()
        )
    assert analysis_insert
    assert not analysis_update
    assert dashboard_read
    assert not capture_read


@pytest.mark.integration
def test_server_is_postgresql_16(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        version = connection.execute("SHOW server_version_num").fetchone()[0]
    assert 160000 <= int(version) < 170000


@pytest.mark.integration
def test_dataset_snapshot_capabilities_are_narrow(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        (
            analysis_read,
            analysis_append,
            analysis_update,
            dashboard_read,
            dashboard_append,
            capture_read,
        ) = connection.execute(
            """
            SELECT has_table_privilege(
                       'leo_analysis', 'dataset_snapshot', 'SELECT'),
                   has_table_privilege(
                       'leo_analysis', 'dataset_member', 'INSERT'),
                   has_table_privilege(
                       'leo_analysis', 'dataset_snapshot', 'UPDATE'),
                   has_table_privilege(
                       'leo_dashboard', 'dataset_snapshot', 'SELECT'),
                   has_table_privilege(
                       'leo_dashboard', 'dataset_member', 'INSERT'),
                   has_table_privilege(
                       'leo_capture', 'dataset_snapshot', 'SELECT')
            """
        ).fetchone()
    assert analysis_read
    assert analysis_append
    assert not analysis_update
    assert dashboard_read
    assert not dashboard_append
    assert not capture_read


@pytest.mark.integration
def test_feature_set_capabilities_are_narrow(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        (
            analysis_read,
            analysis_append,
            analysis_update,
            dashboard_read,
            capture_read,
        ) = connection.execute(
            """
                SELECT has_table_privilege('leo_analysis', 'feature_set', 'SELECT'),
                       has_table_privilege('leo_analysis', 'feature_set', 'INSERT'),
                       has_table_privilege('leo_analysis', 'feature_set', 'UPDATE'),
                       has_table_privilege('leo_dashboard', 'feature_set', 'SELECT'),
                       has_table_privilege('leo_capture', 'feature_set', 'SELECT')
                """
        ).fetchone()
    assert analysis_read
    assert analysis_append
    assert not analysis_update
    assert dashboard_read
    assert not capture_read


@pytest.mark.integration
def test_model_snapshot_and_release_capabilities_are_narrow(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        (
            analysis_read,
            analysis_append,
            analysis_update,
            analysis_sequence_usage,
            dashboard_read,
            dashboard_append,
            dashboard_sequence_usage,
            capture_read,
        ) = connection.execute(
            """
            SELECT has_table_privilege('leo_analysis', 'model_snapshot', 'SELECT'),
                   has_table_privilege('leo_analysis', 'model_release', 'INSERT'),
                   has_table_privilege('leo_analysis', 'model_snapshot', 'UPDATE'),
                   has_sequence_privilege(
                       'leo_analysis', 'model_release_release_sequence_seq', 'USAGE'),
                   has_table_privilege('leo_dashboard', 'model_release', 'SELECT'),
                   has_table_privilege('leo_dashboard', 'model_snapshot', 'INSERT'),
                   has_sequence_privilege(
                       'leo_dashboard', 'model_release_release_sequence_seq', 'USAGE'),
                   has_table_privilege('leo_capture', 'model_snapshot', 'SELECT')
            """
        ).fetchone()
    assert analysis_read
    assert analysis_append
    assert not analysis_update
    assert analysis_sequence_usage
    assert dashboard_read
    assert not dashboard_append
    assert not dashboard_sequence_usage
    assert not capture_read


@pytest.mark.integration
def test_dashboard_projection_capabilities_are_narrow(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        (
            dashboard_read,
            dashboard_write,
            dashboard_sequence_read,
            dashboard_sequence_usage,
            analysis_append,
            analysis_update,
        ) = connection.execute(
            """
                SELECT has_table_privilege(
                           'leo_dashboard', 'dashboard_feature_projection', 'SELECT'),
                       has_table_privilege(
                           'leo_dashboard', 'dashboard_feature_projection', 'INSERT'),
                       has_sequence_privilege(
                           'leo_dashboard', 'dashboard_projection_sequence', 'SELECT'),
                       has_sequence_privilege(
                           'leo_dashboard', 'dashboard_projection_sequence', 'USAGE'),
                       has_table_privilege(
                           'leo_analysis', 'dashboard_feature_projection', 'INSERT'),
                       has_table_privilege(
                           'leo_analysis', 'dashboard_feature_projection', 'UPDATE')
                """
        ).fetchone()
    assert dashboard_read
    assert not dashboard_write
    assert dashboard_sequence_read
    assert not dashboard_sequence_usage
    assert analysis_append
    assert not analysis_update


@pytest.mark.integration
def test_projection_identity_capabilities_are_owner_scoped(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        capture_own, capture_analysis, analysis_own, dashboard_read = (
            connection.execute(
                """
            SELECT has_table_privilege(
                       'leo_capture', 'dashboard_capture_projection_identity', 'INSERT'),
                   has_table_privilege(
                       'leo_capture', 'dashboard_analysis_projection_identity', 'SELECT'),
                   has_table_privilege(
                       'leo_analysis', 'dashboard_analysis_projection_identity', 'INSERT'),
                   has_table_privilege(
                       'leo_dashboard', 'dashboard_capture_projection_identity', 'SELECT')
            """
            ).fetchone()
        )
    assert capture_own
    assert not capture_analysis
    assert analysis_own
    assert not dashboard_read
