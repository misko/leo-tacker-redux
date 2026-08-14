from __future__ import annotations

import secrets
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import psycopg
import pytest

from leo_flow.storage.postgres_migrations import apply_migrations


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    if not _docker_available():
        pytest.skip("Docker daemon unavailable; real PostgreSQL tests require Docker")
    name = f"leo-pg-{uuid.uuid4().hex[:12]}"
    port = _unused_port()
    user = f"leo_test_{uuid.uuid4().hex[:8]}"
    password = secrets.token_urlsafe(24)
    database = "leo_test"
    command = [
        "docker",
        "run",
        "--detach",
        "--rm",
        "--name",
        name,
        "--publish",
        f"127.0.0.1:{port}:5432",
        "--env",
        f"POSTGRES_USER={user}",
        "--env",
        f"POSTGRES_PASSWORD={password}",
        "--env",
        f"POSTGRES_DB={database}",
        "postgres:16-alpine",
    ]
    started = subprocess.run(
        command, check=False, capture_output=True, text=True, timeout=60
    )
    if started.returncode != 0:
        pytest.fail(
            f"failed to start PostgreSQL 16 container: {started.stderr.strip()}"
        )
    dsn = f"postgresql://{user}:{password}@127.0.0.1:{port}/{database}"
    try:
        deadline = time.monotonic() + 45
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(dsn, connect_timeout=2) as connection:
                    connection.execute("SELECT 1")
                break
            except psycopg.Error as error:
                last_error = error
                time.sleep(0.25)
        else:
            pytest.fail(f"PostgreSQL 16 did not become ready: {last_error}")
        with psycopg.connect(dsn) as connection:
            applied = apply_migrations(connection, Path("migrations"))
        assert applied == (
            "0001_first_slice.sql",
            "0002_capability_roles.sql",
            "0003_ephemeris_catalog.sql",
            "0004_dashboard_projections.sql",
            "0005_dataset_snapshots.sql",
            "0006_dashboard_projection_identity.sql",
            "0007_feature_set_catalog.sql",
            "0008_model_snapshot_catalog.sql",
        )
        yield dsn
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )


@pytest.fixture(autouse=True)
def clean_database(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            TRUNCATE dashboard_storage_health_projection,
                     dashboard_analysis_projection_identity,
                     dashboard_capture_projection_identity,
                     dashboard_track_projection, dashboard_model_projection,
                     dashboard_feature_projection, dashboard_activity_projection,
                     dashboard_recording_projection, model_release,
                     model_snapshot, dataset_member, dataset_snapshot,
                     feature_set, ephemeris_snapshot,
                     recording, object_blob, job
            """
        )
