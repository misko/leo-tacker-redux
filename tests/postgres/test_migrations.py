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
        ("0009_recording_ephemeris_link.sql",),
        ("0010_hardware_metadata_catalog.sql",),
        ("0011_recording_hardware_link.sql",),
        ("0012_detector_evaluation_catalog.sql",),
        ("0013_object_retention_gc.sql",),
        ("0014_unregistered_object_reconciliation.sql",),
        ("0015_job_parking.sql",),
        ("0016_tracking_input_catalog.sql",),
        ("0017_security_definer_hardening.sql",),
        ("0018_tracking_model_snapshot_catalog.sql",),
        ("0019_dwell_request_ingress.sql",),
        ("0020_feature_projection_work.sql",),
        ("0021_dashboard_capture_batch_projection.sql",),
        ("0022_analysis_migration_receipt_read.sql",),
        ("0023_campaign_projection_receipt.sql",),
        ("0024_capture_analysis_inactive.sql",),
        ("0025_recording_waterfall_analysis.sql",),
        ("0026_dashboard_recording_detail_waterfall_projection.sql",),
        ("0027_capture_analysis_waterfall_drain.sql",),
        ("0028_recording_starlink_candidate_pipeline.sql",),
        ("0029_starlink_detector_suite_v0_2.sql",),
        ("0030_campaign_scoped_analysis_claims.sql",),
        ("0031_radio_lifecycle_detection.sql",),
        ("0032_campaign_online_analysis.sql",),
        ("0033_registered_analysis_during_capture.sql",),
        ("0034_waterfall_v0_2_doppler_analysis.sql",),
        ("0035_starlink_surrogate_null_catalog.sql",),
        ("0036_starlink_pilot_constellation_catalog.sql",),
        ("0037_focused_analysis_during_capture.sql",),
        ("0038_dashboard_surrogate_score_distributions.sql",),
        ("0039_starlink_temporal_pilot_catalog.sql",),
        ("0040_dashboard_doppler_aggregate.sql",),
        ("0041_starlink_full_dwell_response_v0_1.sql",),
        ("0042_starlink_full_dwell_work.sql",),
        ("0043_starlink_acquired_qam_v0_3.sql",),
        ("0044_prompt_full_dwell_timeline.sql",),
        ("0045_prompt_timeline_source_acl.sql",),
        ("0046_starlink_adaptive_response_v0_1.sql",),
        ("0047_starlink_adaptive_qam_v0_4.sql",),
        ("0048_starlink_pilot_prescreen_v0_1.sql",),
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
def test_analysis_can_read_only_migration_receipts(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        analysis_select, analysis_insert, capture_select, dashboard_select = (
            connection.execute(
                """
                SELECT has_table_privilege(
                           'leo_analysis', 'schema_migration', 'SELECT'),
                       has_table_privilege(
                           'leo_analysis', 'schema_migration', 'INSERT'),
                       has_table_privilege(
                           'leo_capture', 'schema_migration', 'SELECT'),
                       has_table_privilege(
                           'leo_dashboard', 'schema_migration', 'SELECT')
                """
            ).fetchone()
        )
        connection.execute("SET ROLE leo_analysis")
        receipts = connection.execute(
            "SELECT name FROM schema_migration ORDER BY name"
        ).fetchall()

    assert analysis_select
    assert not analysis_insert
    assert not capture_select
    assert not dashboard_select
    assert receipts[-1] == ("0048_starlink_pilot_prescreen_v0_1.sql",)


@pytest.mark.integration
def test_prompt_timeline_capabilities_are_narrow(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        table_acl = connection.execute(
            """
            SELECT has_table_privilege('leo_analysis','recording_full_dwell_timeline_v0_1','SELECT'),
                   has_table_privilege('leo_dashboard','recording_full_dwell_timeline_v0_1','SELECT'),
                   has_table_privilege('leo_capture','recording_full_dwell_timeline_v0_1','SELECT'),
                   has_table_privilege('leo_analysis','full_dwell_timeline_work_v0_1','SELECT')
            """
        ).fetchone()
        function_acl = connection.execute(
            """
            SELECT has_function_privilege('leo_analysis','admit_full_dwell_timeline_work_v0_1(jsonb,jsonb)','EXECUTE'),
                   has_function_privilege('leo_dashboard','admit_full_dwell_timeline_work_v0_1(jsonb,jsonb)','EXECUTE'),
                   has_function_privilege('leo_capture','admit_full_dwell_timeline_work_v0_1(jsonb,jsonb)','EXECUTE'),
                   has_function_privilege('leo_dashboard','read_latest_recording_full_dwell_timeline_v0_1(text)','EXECUTE')
            """
        ).fetchone()
        routine_source_acl = connection.execute(
            """
            SELECT has_column_privilege('leo_routine_owner','recording','recording_id','SELECT'),
                   has_column_privilege('leo_routine_owner','recording','published_at','SELECT'),
                   has_table_privilege('leo_routine_owner','recording','SELECT')
            """
        ).fetchone()
    assert table_acl == (False, False, False, False)
    assert function_acl == (True, False, False, True)
    assert routine_source_acl == (True, True, False)


@pytest.mark.integration
def test_adaptive_qam_catalog_capabilities_are_directional(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        table_acl = connection.execute(
            """
            SELECT has_table_privilege('leo_analysis','recording_starlink_adaptive_qam_v0_4','SELECT'),
                   has_table_privilege('leo_dashboard','recording_starlink_adaptive_qam_v0_4','SELECT'),
                   has_table_privilege('leo_capture','recording_starlink_adaptive_qam_v0_4','SELECT')
            """
        ).fetchone()
        function_acl = connection.execute(
            """
            SELECT has_function_privilege('leo_analysis','publish_recording_starlink_adaptive_qam_v0_4(jsonb)','EXECUTE'),
                   has_function_privilege('leo_dashboard','publish_recording_starlink_adaptive_qam_v0_4(jsonb)','EXECUTE'),
                   has_function_privilege('leo_capture','publish_recording_starlink_adaptive_qam_v0_4(jsonb)','EXECUTE'),
                   has_function_privilege('leo_dashboard','read_latest_recording_starlink_adaptive_qam_v0_4(text)','EXECUTE'),
                   has_function_privilege('leo_analysis','read_latest_recording_starlink_adaptive_qam_v0_4(text)','EXECUTE')
            """
        ).fetchone()
    assert table_acl == (False, False, False)
    assert function_acl == (True, False, False, True, True)


@pytest.mark.integration
def test_dwell_ingress_capabilities_are_directional(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        row = connection.execute(
            """
            SELECT has_table_privilege(
                       'leo_analysis', 'dwell_request_ingress', 'SELECT'),
                   has_table_privilege(
                       'leo_capture', 'dwell_request_ingress', 'SELECT'),
                   has_function_privilege(
                       'leo_analysis', 'publish_dwell_request(jsonb)', 'EXECUTE'),
                   has_function_privilege(
                       'leo_capture', 'publish_dwell_request(jsonb)', 'EXECUTE'),
                   has_function_privilege(
                       'leo_capture',
                       'claim_dwell_request(text,text,text,interval)', 'EXECUTE'),
                   has_function_privilege(
                       'leo_analysis',
                       'claim_dwell_request(text,text,text,interval)', 'EXECUTE'),
                   has_function_privilege(
                       'leo_dashboard',
                       'claim_dwell_request(text,text,text,interval)', 'EXECUTE')
            """
        ).fetchone()
    assert row == (False, False, True, False, True, False, False)


@pytest.mark.integration
def test_feature_projection_work_capabilities_are_private_and_narrow(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        table_row = connection.execute(
            """
            SELECT has_table_privilege(
                       'leo_analysis', 'feature_projection_work', 'SELECT'),
                   has_table_privilege(
                       'leo_analysis', 'feature_projection_work', 'INSERT'),
                   has_table_privilege(
                       'leo_dashboard', 'feature_projection_work', 'SELECT'),
                   has_table_privilege(
                       'leo_capture', 'feature_projection_work', 'SELECT')
            """
        ).fetchone()
        function_rows = connection.execute(
            """
            SELECT function_name,
                   has_function_privilege('leo_analysis', signature, 'EXECUTE'),
                   has_function_privilege('leo_dashboard', signature, 'EXECUTE'),
                   has_function_privilege('leo_capture', signature, 'EXECUTE')
              FROM (VALUES
                  ('publish',
                   'publish_feature_projection_work(text,text,text,bigint,text,text,text,text,text,text,text)'),
                  ('claim', 'claim_feature_projection_work(text,interval)'),
                  ('heartbeat',
                   'heartbeat_feature_projection_work(text,text,bigint,interval)'),
                  ('complete',
                   'complete_feature_projection_work(text,text,bigint)'),
                  ('retry',
                   'retry_feature_projection_work(text,text,bigint,text,interval)'),
                  ('park', 'park_feature_projection_work(text,text,bigint,text)')
              ) AS expected(function_name, signature)
             ORDER BY function_name
            """
        ).fetchall()
    assert table_row == (False, False, False, False)
    assert function_rows == [
        ("claim", True, False, False),
        ("complete", True, False, False),
        ("heartbeat", True, False, False),
        ("park", True, False, False),
        ("publish", True, False, False),
        ("retry", True, False, False),
    ]


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
def test_ephemeris_link_capabilities_are_narrow(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        (
            analysis_read,
            analysis_insert,
            analysis_update,
            dashboard_read,
            capture_read,
        ) = connection.execute(
            """
                SELECT has_table_privilege(
                           'leo_analysis', 'recording_ephemeris_link', 'SELECT'),
                       has_table_privilege(
                           'leo_analysis', 'recording_ephemeris_link', 'INSERT'),
                       has_table_privilege(
                           'leo_analysis', 'recording_ephemeris_link', 'UPDATE'),
                       has_table_privilege(
                           'leo_dashboard', 'recording_ephemeris_link', 'SELECT'),
                       has_table_privilege(
                           'leo_capture', 'recording_ephemeris_link', 'SELECT')
                """
        ).fetchone()
    assert analysis_read and analysis_insert
    assert not analysis_update
    assert dashboard_read
    assert not capture_read


@pytest.mark.integration
def test_hardware_metadata_capabilities_are_narrow(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        (
            analysis_read,
            analysis_append,
            analysis_update,
            capture_read,
            capture_append,
            dashboard_read,
        ) = connection.execute(
            """
            SELECT has_table_privilege(
                       'leo_analysis', 'hardware_snapshot', 'SELECT'),
                   has_table_privilege(
                       'leo_analysis', 'hardware_receiver_chain', 'INSERT'),
                   has_table_privilege(
                       'leo_analysis', 'hardware_radio', 'UPDATE'),
                   has_table_privilege(
                       'leo_capture', 'hardware_snapshot', 'SELECT'),
                   has_table_privilege(
                       'leo_capture', 'hardware_receiver_chain', 'INSERT'),
                   has_table_privilege(
                       'leo_dashboard', 'hardware_radio', 'SELECT')
            """
        ).fetchone()
    assert analysis_read and analysis_append
    assert not analysis_update
    assert capture_read and not capture_append
    assert dashboard_read


@pytest.mark.integration
def test_recording_hardware_link_capabilities_are_narrow(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        values = connection.execute(
            """
            SELECT has_table_privilege(
                       'leo_analysis', 'recording_hardware_link', 'SELECT'),
                   has_table_privilege(
                       'leo_analysis', 'recording_hardware_link', 'INSERT'),
                   has_table_privilege(
                       'leo_analysis', 'recording_hardware_link', 'UPDATE'),
                   has_table_privilege(
                       'leo_dashboard', 'recording_hardware_link', 'SELECT'),
                   has_table_privilege(
                       'leo_capture', 'recording_hardware_link', 'SELECT')
            """
        ).fetchone()
    assert values == (True, True, False, True, False)


@pytest.mark.integration
def test_detector_evaluation_capabilities_are_narrow(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        values = connection.execute(
            """
            SELECT has_table_privilege('leo_analysis', 'detector_evaluation_report', 'SELECT'),
                   has_table_privilege('leo_analysis', 'detector_evaluation_method_summary', 'INSERT'),
                   has_table_privilege('leo_analysis', 'detector_evaluation_report', 'UPDATE'),
                   has_table_privilege('leo_dashboard', 'detector_evaluation_report', 'SELECT'),
                   has_table_privilege('leo_dashboard', 'detector_evaluation_method_summary', 'INSERT'),
                   has_table_privilege('leo_capture', 'detector_evaluation_report', 'SELECT')
            """
        ).fetchone()
    assert values == (True, True, False, True, False, False)


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
