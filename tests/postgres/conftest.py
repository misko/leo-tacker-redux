from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from leo_flow.storage.postgres_migrations import apply_migrations

_EXPECTED_MIGRATIONS = (
    "0001_first_slice.sql",
    "0002_capability_roles.sql",
    "0003_ephemeris_catalog.sql",
    "0004_dashboard_projections.sql",
    "0005_dataset_snapshots.sql",
    "0006_dashboard_projection_identity.sql",
    "0007_feature_set_catalog.sql",
    "0008_model_snapshot_catalog.sql",
    "0009_recording_ephemeris_link.sql",
    "0010_hardware_metadata_catalog.sql",
    "0011_recording_hardware_link.sql",
    "0012_detector_evaluation_catalog.sql",
    "0013_object_retention_gc.sql",
    "0014_unregistered_object_reconciliation.sql",
    "0015_job_parking.sql",
    "0016_tracking_input_catalog.sql",
    "0017_security_definer_hardening.sql",
    "0018_tracking_model_snapshot_catalog.sql",
    "0019_dwell_request_ingress.sql",
    "0020_feature_projection_work.sql",
    "0021_dashboard_capture_batch_projection.sql",
    "0022_analysis_migration_receipt_read.sql",
    "0023_campaign_projection_receipt.sql",
    "0024_capture_analysis_inactive.sql",
    "0025_recording_waterfall_analysis.sql",
    "0026_dashboard_recording_detail_waterfall_projection.sql",
    "0027_capture_analysis_waterfall_drain.sql",
    "0028_recording_starlink_candidate_pipeline.sql",
    "0029_starlink_detector_suite_v0_2.sql",
    "0030_campaign_scoped_analysis_claims.sql",
    "0031_radio_lifecycle_detection.sql",
    "0032_campaign_online_analysis.sql",
    "0033_registered_analysis_during_capture.sql",
    "0034_waterfall_v0_2_doppler_analysis.sql",
    "0035_starlink_surrogate_null_catalog.sql",
    "0036_starlink_pilot_constellation_catalog.sql",
    "0037_focused_analysis_during_capture.sql",
    "0038_dashboard_surrogate_score_distributions.sql",
    "0039_starlink_temporal_pilot_catalog.sql",
    "0040_dashboard_doppler_aggregate.sql",
    "0041_starlink_full_dwell_response_v0_1.sql",
    "0042_starlink_full_dwell_work.sql",
    "0043_starlink_acquired_qam_v0_3.sql",
)


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


def _wait_for_postgres(dsn: str) -> None:
    deadline = time.monotonic() + 45
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=2) as connection:
                version_row = connection.execute("SHOW server_version_num").fetchone()
            if version_row is None:
                pytest.fail("PostgreSQL did not return server_version_num")
            version_num = int(version_row[0])
            if version_num // 10_000 != 16:
                pytest.fail(
                    "real PostgreSQL tests require server major 16; "
                    f"connected to major {version_num // 10_000}"
                )
            return
        except psycopg.Error as error:
            last_error = error
            time.sleep(0.25)
    pytest.fail(f"PostgreSQL 16 did not become ready: {last_error}")


def _assert_exact_migrations(dsn: str, migration_directory: Path) -> None:
    expected = tuple(
        (name, hashlib.sha256((migration_directory / name).read_bytes()).hexdigest())
        for name in _EXPECTED_MIGRATIONS
    )
    with psycopg.connect(dsn) as connection:
        actual = tuple(
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT name, sha256 FROM schema_migration ORDER BY name"
            ).fetchall()
        )
    if actual != expected:
        pytest.fail("PostgreSQL migration receipts do not exactly match the repository")


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    external_dsn = os.environ.get("LEO_TEST_POSTGRES_DSN")
    container_name: str | None = None
    if external_dsn:
        dsn = external_dsn
    else:
        if not _docker_available():
            pytest.skip(
                "Docker daemon unavailable; set LEO_TEST_POSTGRES_DSN to an "
                "externally managed disposable PostgreSQL 16 database"
            )
        container_name = f"leo-pg-{uuid.uuid4().hex[:12]}"
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
            container_name,
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
        _wait_for_postgres(dsn)
        migration_directory = Path("migrations")
        with psycopg.connect(dsn) as connection:
            apply_migrations(connection, migration_directory)
        _assert_exact_migrations(dsn, migration_directory)
        yield dsn
    finally:
        if container_name is not None:
            subprocess.run(
                ["docker", "rm", "--force", container_name],
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
            TRUNCATE recording_starlink_acquired_constellation_v0_3,
                     recording_starlink_full_dwell_point_v0_1,
                     recording_starlink_full_dwell_v0_1,
                     focused_analysis_pair_job_scope,
                     focused_analysis_pair_scope,
                     recording_starlink_pilot_constellation,
                     recording_starlink_surrogate_null,
                     recording_starlink_temporal_pilot,
                     recording_doppler_analysis,
                     recording_waterfall_v0_2,
                     campaign_analysis_job_scope,
                     campaign_analysis_window_scope,
                     dashboard_recording_starlink_detector_suite_projection,
                     radio_lifecycle_interval_fact,
                     capture_attempt_radio_lifecycle_fact,
                     starlink_detector_suite_projection_work,
                     recording_starlink_detector_suite,
                     dashboard_recording_starlink_projection,
                     starlink_projection_work, recording_starlink_candidate,
                     dashboard_recording_waterfall_projection,
                     dashboard_recording_detail_projection,
                     waterfall_projection_work, recording_waterfall,
                     dashboard_capture_attempt_projection,
                     dashboard_capture_batch_projection,
                     feature_projection_work, dwell_request_ingress,
                     tracking_model_snapshot,
                     tracking_input_entry, tracking_input_snapshot,
                     object_orphan_event, object_orphan_observation,
                     object_gc_attempt, object_retention_assignment,
                     object_retention_policy,
                     detector_evaluation_method_summary,
                     detector_evaluation_report,
                     recording_hardware_link, recording_ephemeris_link,
                     hardware_receiver_chain,
                     hardware_radio, hardware_snapshot,
                     dashboard_storage_health_projection,
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
