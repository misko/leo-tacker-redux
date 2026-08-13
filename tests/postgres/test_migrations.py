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
