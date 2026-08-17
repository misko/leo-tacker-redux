from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest

DEFINER_FUNCTIONS = {
    "claim_campaign_analysis_job",
    "claim_campaign_feature_projection",
    "claim_campaign_starlink_suite_projection",
    "claim_campaign_waterfall_projection",
    "claim_dwell_request",
    "claim_feature_projection_work",
    "claim_job",
    "claim_unregistered_object",
    "claim_waterfall_projection_work",
    "claim_starlink_projection_work",
    "claim_starlink_detector_suite_projection_work",
    "capture_analysis_drain_ready",
    "capture_analysis_inactive",
    "capture_campaign_analysis_safe_v1",
    "capture_registered_analysis_safe_v2",
    "complete_dwell_request",
    "complete_feature_projection_work",
    "complete_job",
    "complete_unregistered_object_delete",
    "complete_waterfall_projection_work",
    "complete_starlink_projection_work",
    "complete_starlink_detector_suite_projection_work",
    "enqueue_job",
    "fail_job",
    "fail_dwell_request",
    "gc_claim_object",
    "gc_complete_object_delete",
    "gc_record_delete_failure",
    "heartbeat_job",
    "heartbeat_dwell_request",
    "heartbeat_feature_projection_work",
    "heartbeat_waterfall_projection_work",
    "lock_active_job_lease",
    "object_blob_assert_live_reference",
    "object_digest_fence",
    "observe_unregistered_object",
    "orphan_claim_is_current",
    "park_job",
    "park_dwell_request",
    "park_feature_projection_work",
    "park_waterfall_projection_work",
    "park_starlink_projection_work",
    "park_starlink_detector_suite_projection_work",
    "publish_dashboard_capture_batch",
    "publish_capture_attempt_radio_lifecycle_fact",
    "publish_radio_lifecycle_interval_fact",
    "publish_dashboard_recording_detail",
    "publish_dashboard_recording_waterfall",
    "publish_dashboard_recording_starlink",
    "publish_dashboard_recording_starlink_detector_suite",
    "publish_feature_projection_work",
    "publish_recording_waterfall",
    "publish_recording_starlink_candidate",
    "publish_recording_starlink_detector_suite",
    "publish_recording_starlink_surrogate_null",
    "publish_recording_starlink_pilot_constellation",
    "publish_recording_doppler_analysis",
    "publish_recording_waterfall_v0_2",
    "publish_starlink_projection_work",
    "publish_starlink_detector_suite_projection_work",
    "publish_waterfall_projection_work",
    "read_feature_projection_receipt",
    "read_capture_attempt_radio_lifecycle_fact",
    "read_latest_radio_lifecycle_terminal",
    "read_campaign_analysis_lane_status",
    "read_waterfall_analysis_receipt",
    "read_starlink_analysis_receipt",
    "read_starlink_detector_suite_receipt",
    "read_recording_doppler_analysis",
    "read_recording_waterfall_v0_2",
    "read_recording_starlink_surrogate_null",
    "read_latest_recording_starlink_surrogate_null",
    "read_recording_starlink_pilot_constellation",
    "read_latest_recording_starlink_pilot_constellation",
    "publish_tracking_input_snapshot",
    "publish_tracking_model_snapshot",
    "publish_dwell_request",
    "record_unregistered_object_delete_failure",
    "register_live_object_blob",
    "register_campaign_analysis_window_scope_v1",
    "retry_feature_projection_work",
    "retry_waterfall_projection_work",
    "retry_starlink_projection_work",
    "retry_starlink_detector_suite_projection_work",
    "resolve_dashboard_capture_batches_for_recording",
}

ANALYSIS_FUNCTIONS = {
    "claim_campaign_analysis_job",
    "claim_campaign_feature_projection",
    "claim_campaign_starlink_suite_projection",
    "claim_campaign_waterfall_projection",
    "claim_feature_projection_work",
    "claim_job",
    "claim_waterfall_projection_work",
    "claim_starlink_projection_work",
    "claim_starlink_detector_suite_projection_work",
    "complete_feature_projection_work",
    "complete_job",
    "complete_waterfall_projection_work",
    "complete_starlink_projection_work",
    "complete_starlink_detector_suite_projection_work",
    "enqueue_job",
    "fail_job",
    "heartbeat_job",
    "heartbeat_feature_projection_work",
    "heartbeat_waterfall_projection_work",
    "lock_active_job_lease",
    "park_job",
    "park_feature_projection_work",
    "park_waterfall_projection_work",
    "park_starlink_projection_work",
    "park_starlink_detector_suite_projection_work",
    "publish_dashboard_capture_batch",
    "publish_dashboard_recording_waterfall",
    "publish_dashboard_recording_starlink",
    "publish_dashboard_recording_starlink_detector_suite",
    "publish_feature_projection_work",
    "publish_recording_waterfall",
    "publish_recording_starlink_candidate",
    "publish_recording_starlink_detector_suite",
    "publish_recording_starlink_surrogate_null",
    "publish_recording_starlink_pilot_constellation",
    "publish_recording_doppler_analysis",
    "publish_recording_waterfall_v0_2",
    "publish_starlink_projection_work",
    "publish_starlink_detector_suite_projection_work",
    "publish_waterfall_projection_work",
    "read_feature_projection_receipt",
    "read_campaign_analysis_lane_status",
    "read_waterfall_analysis_receipt",
    "read_starlink_analysis_receipt",
    "read_starlink_detector_suite_receipt",
    "read_recording_doppler_analysis",
    "read_recording_waterfall_v0_2",
    "read_recording_starlink_surrogate_null",
    "read_latest_recording_starlink_surrogate_null",
    "read_recording_starlink_pilot_constellation",
    "read_latest_recording_starlink_pilot_constellation",
    "publish_tracking_input_snapshot",
    "publish_tracking_model_snapshot",
    "publish_dwell_request",
    "register_live_object_blob",
    "register_campaign_analysis_window_scope_v1",
    "retry_feature_projection_work",
    "retry_waterfall_projection_work",
    "retry_starlink_projection_work",
    "retry_starlink_detector_suite_projection_work",
    "resolve_dashboard_capture_batches_for_recording",
}
CAPTURE_FUNCTIONS = {
    "capture_analysis_drain_ready",
    "capture_analysis_inactive",
    "capture_campaign_analysis_safe_v1",
    "capture_registered_analysis_safe_v2",
    "claim_dwell_request",
    "complete_dwell_request",
    "fail_dwell_request",
    "heartbeat_dwell_request",
    "park_dwell_request",
    "publish_dashboard_capture_batch",
    "publish_capture_attempt_radio_lifecycle_fact",
    "publish_radio_lifecycle_interval_fact",
    "read_latest_radio_lifecycle_terminal",
    "publish_dashboard_recording_detail",
    "read_waterfall_analysis_receipt",
    "register_live_object_blob",
}
DASHBOARD_FUNCTIONS = {
    "read_capture_attempt_radio_lifecycle_fact",
    "read_recording_doppler_analysis",
    "read_recording_waterfall_v0_2",
    "read_recording_starlink_surrogate_null",
    "read_latest_recording_starlink_surrogate_null",
    "read_recording_starlink_pilot_constellation",
    "read_latest_recording_starlink_pilot_constellation",
}
MAINTENANCE_FUNCTIONS = {
    "claim_unregistered_object",
    "complete_unregistered_object_delete",
    "gc_claim_object",
    "gc_complete_object_delete",
    "gc_record_delete_failure",
    "observe_unregistered_object",
    "orphan_claim_is_current",
    "record_unregistered_object_delete_failure",
}


@contextmanager
def _role_connection(postgres_dsn: str, role: str) -> Iterator[psycopg.Connection]:
    connection = psycopg.connect(postgres_dsn, autocommit=True)
    try:
        connection.execute(f"SET ROLE {role}")
        connection.execute("SET search_path = pg_temp, public")
        yield connection
    finally:
        connection.close()


def _install_rejecting_shadow(
    connection: psycopg.Connection,
    table_name: str,
    *,
    like_name: str | None = None,
) -> None:
    if like_name is None:
        connection.execute(f"CREATE TEMP TABLE {table_name} (shadow text)")
    else:
        connection.execute(
            f"CREATE TEMP TABLE {table_name} (LIKE public.{like_name} INCLUDING ALL)"
        )
    connection.execute(
        f"""
        CREATE TRIGGER reject_{table_name}_shadow
        BEFORE INSERT OR UPDATE OR DELETE ON {table_name}
        FOR EACH ROW EXECUTE FUNCTION pg_temp.reject_shadow_write()
        """
    )


@pytest.mark.integration
def test_definer_owner_search_path_schema_and_execute_acl_inventory(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        owner = connection.execute(
            """
            SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
                   rolinherit, rolreplication, rolbypassrls
              FROM pg_roles WHERE rolname = 'leo_routine_owner'
            """
        ).fetchone()
        assert owner == (False, False, False, False, False, False, False)
        rows = connection.execute(
            """
            SELECT p.oid, p.proname, owner.rolname, p.prosecdef, p.proconfig,
                   has_function_privilege('leo_analysis', p.oid, 'EXECUTE'),
                   has_function_privilege('leo_capture', p.oid, 'EXECUTE'),
                   has_function_privilege('leo_dashboard', p.oid, 'EXECUTE'),
                   has_function_privilege('leo_maintenance', p.oid, 'EXECUTE')
              FROM pg_proc AS p
              JOIN pg_namespace AS n ON n.oid = p.pronamespace
              JOIN pg_roles AS owner ON owner.oid = p.proowner
             WHERE n.nspname = 'public' AND p.prosecdef
             ORDER BY p.proname
            """
        ).fetchall()
        assert {row[1] for row in rows} == DEFINER_FUNCTIONS
        for (
            _,
            name,
            owner_name,
            security_definer,
            config,
            analysis,
            capture,
            dashboard,
            maintenance,
        ) in rows:
            assert owner_name == "leo_routine_owner"
            assert security_definer is True
            assert config == ["search_path=pg_catalog, pg_temp"]
            assert analysis is (name in ANALYSIS_FUNCTIONS)
            assert capture is (name in CAPTURE_FUNCTIONS)
            assert dashboard is (name in DASHBOARD_FUNCTIONS)
            assert maintenance is (name in MAINTENANCE_FUNCTIONS)

        schema_and_membership = connection.execute(
            """
            SELECT has_schema_privilege('leo_analysis', 'public', 'CREATE'),
                   has_schema_privilege('leo_capture', 'public', 'CREATE'),
                   has_schema_privilege('leo_dashboard', 'public', 'CREATE'),
                   has_schema_privilege('leo_maintenance', 'public', 'CREATE'),
                   pg_has_role('leo_analysis', 'leo_routine_owner', 'MEMBER'),
                   pg_has_role('leo_maintenance', 'leo_routine_owner', 'MEMBER')
            """
        ).fetchone()
        assert schema_and_membership == (False, False, False, False, False, False)
        owner_privileges = connection.execute(
            """
            SELECT has_table_privilege('leo_routine_owner', 'object_blob', 'SELECT'),
                   has_table_privilege('leo_routine_owner', 'object_blob', 'UPDATE'),
                   has_table_privilege('leo_routine_owner', 'object_blob', 'DELETE'),
                   has_table_privilege('leo_routine_owner', 'job', 'INSERT'),
                   has_table_privilege('leo_routine_owner', 'job', 'UPDATE'),
                   has_table_privilege('leo_routine_owner', 'job', 'DELETE'),
                   has_table_privilege('leo_routine_owner', 'recording', 'SELECT'),
                   has_table_privilege(
                       'leo_routine_owner', 'tracking_input_snapshot', 'INSERT')
            """
        ).fetchone()
        assert owner_privileges == (True, True, False, True, True, False, False, True)


@pytest.mark.integration
def test_temp_object_blob_cannot_bypass_live_reference_trigger(
    postgres_dsn: str,
) -> None:
    data_digest = "1" * 64
    metadata_digest = "2" * 64
    with psycopg.connect(postgres_dsn) as connection:
        for digest in (data_digest, metadata_digest):
            connection.execute(
                """
                INSERT INTO object_blob
                    (digest_algorithm, digest_value, byte_count, media_type,
                     format_id, locator, lifecycle_state, gc_claim_token,
                     gc_claimed_at, gc_claim_expires_at)
                VALUES ('sha256', %s, 1, 'application/octet-stream', 'fixture-v1',
                        %s, 'gc_claimed', 'claim', clock_timestamp(),
                        clock_timestamp() + interval '5 minutes')
                """,
                (digest, f"cas:sha256:{digest}"),
            )

    with _role_connection(postgres_dsn, "leo_capture") as connection:
        connection.execute(
            "CREATE TEMP TABLE object_blob (LIKE public.object_blob INCLUDING ALL)"
        )
        for digest in (data_digest, metadata_digest):
            connection.execute(
                """
                INSERT INTO object_blob
                    (digest_algorithm, digest_value, byte_count, media_type,
                     format_id, locator, lifecycle_state)
                VALUES ('sha256', %s, 1, 'application/octet-stream', 'fixture-v1',
                        %s, 'live')
                """,
                (digest, f"temp:{digest}"),
            )
        connection.execute(
            """
            CREATE FUNCTION pg_temp.reject_shadow_write() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'shadow table used'; END $$
            """
        )
        connection.execute(
            """
            CREATE TRIGGER reject_object_blob_shadow
            BEFORE INSERT OR UPDATE OR DELETE ON object_blob
            FOR EACH ROW EXECUTE FUNCTION pg_temp.reject_shadow_write()
            """
        )
        registration_digest = "6" * 64
        connection.execute(
            """
            SELECT public.register_live_object_blob(
                'sha256', %s, 1, 'application/octet-stream', 'fixture-v1', %s)
            """,
            (registration_digest, f"cas:sha256:{registration_digest}"),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                """
                INSERT INTO public.recording
                    (recording_id, data_digest_algorithm, data_digest_value,
                     metadata_digest_algorithm, metadata_digest_value,
                     manifest_digest_value, idempotency_key, state)
                VALUES ('rec_shadow_attack', 'sha256', %s, 'sha256', %s,
                        %s, 'shadow-attack', 'published')
                """,
                (data_digest, metadata_digest, "3" * 64),
            )
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM recording WHERE recording_id='rec_shadow_attack'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT lifecycle_state FROM object_blob WHERE digest_value = %s",
            ("6" * 64,),
        ).fetchone() == ("live",)


@pytest.mark.integration
def test_temp_job_table_and_trigger_cannot_intercept_job_state_machine(
    postgres_dsn: str,
) -> None:
    with _role_connection(postgres_dsn, "leo_analysis") as connection:
        connection.execute(
            """
            CREATE FUNCTION pg_temp.reject_shadow_write() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'shadow table used'; END $$
            """
        )
        _install_rejecting_shadow(connection, "job", like_name="job")

        def enqueue(job_id: str) -> None:
            assert connection.execute(
                "SELECT public.enqueue_job(%s, 'security-test', 'schema', '0.1', '{}'::jsonb, clock_timestamp())",
                (job_id,),
            ).fetchone() == (True,)

        def claim(job_id: str, token: str) -> int:
            row = connection.execute(
                """
                SELECT job_id, lease_generation
                  FROM public.claim_job(ARRAY['security-test'], %s, interval '5 minutes')
                """,
                (token,),
            ).fetchone()
            assert row is not None and row[0] == job_id
            return int(row[1])

        enqueue("job_shadow_complete")
        generation = claim("job_shadow_complete", "lease-complete")
        assert connection.execute(
            "SELECT public.lock_active_job_lease(%s, 'security-test', %s, %s)",
            ("job_shadow_complete", "lease-complete", generation),
        ).fetchone() == (True,)
        assert connection.execute(
            """
            SELECT count(*) FROM public.heartbeat_job(
                %s, %s, %s, interval '5 minutes')
            """,
            ("job_shadow_complete", "lease-complete", generation),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM public.complete_job(%s, %s, %s, '{}'::jsonb)",
            ("job_shadow_complete", "lease-complete", generation),
        ).fetchone() == (1,)

        enqueue("job_shadow_fail")
        generation = claim("job_shadow_fail", "lease-fail")
        assert connection.execute(
            """
            SELECT count(*) FROM public.fail_job(
                %s, %s, %s, 'retry', clock_timestamp() + interval '1 hour')
            """,
            ("job_shadow_fail", "lease-fail", generation),
        ).fetchone() == (1,)

        enqueue("job_shadow_park")
        generation = claim("job_shadow_park", "lease-park")
        assert connection.execute(
            "SELECT count(*) FROM public.park_job(%s, %s, %s, 'unsupported.schema')",
            ("job_shadow_park", "lease-park", generation),
        ).fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM pg_temp.job").fetchone() == (0,)

    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT job_id, state FROM job ORDER BY job_id"
        ).fetchall() == [
            ("job_shadow_complete", "succeeded"),
            ("job_shadow_fail", "failed"),
            ("job_shadow_park", "parked"),
        ]


@pytest.mark.integration
def test_temp_orphan_tables_functions_and_triggers_cannot_intercept_reconciliation(
    postgres_dsn: str,
) -> None:
    digest = "4" * 64
    evidence = ("sha256", digest, 8, f"cas:sha256:{digest}", 1, 2, 3, 4, 5)
    with _role_connection(postgres_dsn, "leo_maintenance") as connection:
        connection.execute(
            """
            CREATE FUNCTION pg_temp.reject_shadow_write() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'shadow table used'; END $$
            """
        )
        connection.execute(
            """
            CREATE FUNCTION pg_temp.object_digest_fence(text, text) RETURNS void
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'shadow fence used'; END $$
            """
        )
        _install_rejecting_shadow(
            connection,
            "object_orphan_observation",
            like_name="object_orphan_observation",
        )
        _install_rejecting_shadow(
            connection, "object_orphan_event", like_name="object_orphan_event"
        )
        _install_rejecting_shadow(connection, "object_blob", like_name="object_blob")
        assert connection.execute(
            "SELECT public.observe_unregistered_object(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            evidence,
        ).fetchone() == ("unregistered",)
        with psycopg.connect(postgres_dsn) as owner:
            owner.execute(
                """
                UPDATE object_orphan_observation
                   SET first_observed_at = clock_timestamp() - interval '2 seconds'
                 WHERE digest_value = %s
                """,
                (digest,),
            )
        assert connection.execute(
            "SELECT public.claim_unregistered_object(%s,%s,%s,%s,%s,%s,%s,%s,%s,'claim',1)",
            evidence,
        ).fetchone() == ("claim",)
        assert connection.execute(
            "SELECT public.orphan_claim_is_current(%s,%s,%s,%s,%s,%s,%s,%s,%s,'claim')",
            evidence,
        ).fetchone() == (True,)
        assert connection.execute(
            "SELECT public.record_unregistered_object_delete_failure('sha256',%s,'claim','ambiguous')",
            (digest,),
        ).fetchone() == (True,)
        assert connection.execute(
            "SELECT public.complete_unregistered_object_delete('sha256',%s,'claim')",
            (digest,),
        ).fetchone() == (True,)
        assert connection.execute(
            "SELECT count(*) FROM pg_temp.object_orphan_observation"
        ).fetchone() == (0,)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT state FROM object_orphan_observation WHERE digest_value=%s",
            (digest,),
        ).fetchone() == ("deleted",)


@pytest.mark.integration
def test_temp_gc_status_and_tables_cannot_intercept_fenced_gc(
    postgres_dsn: str,
) -> None:
    digest = "5" * 64
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            INSERT INTO object_blob
                (digest_algorithm, digest_value, byte_count, media_type, format_id, locator)
            VALUES ('sha256', %s, 8, 'application/octet-stream', 'fixture-v1', %s)
            """,
            (digest, f"cas:sha256:{digest}"),
        )
        connection.execute(
            """
            INSERT INTO object_retention_policy
                (policy_id, retain_for_seconds, grace_period_seconds,
                 allow_remote_delete, rationale)
            VALUES ('security-test', 0, 1, true, 'security hardening test')
            """
        )
        connection.execute(
            """
            INSERT INTO object_retention_assignment
                (digest_algorithm, digest_value, policy_id, assigned_at, assigned_by)
            VALUES ('sha256', %s, 'security-test',
                    clock_timestamp() - interval '2 seconds', 'test')
            """,
            (digest,),
        )

    with _role_connection(postgres_dsn, "leo_maintenance") as connection:
        connection.execute(
            """
            CREATE FUNCTION pg_temp.reject_shadow_write() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'shadow table used'; END $$
            """
        )
        _install_rejecting_shadow(connection, "object_blob", like_name="object_blob")
        _install_rejecting_shadow(
            connection, "object_gc_attempt", like_name="object_gc_attempt"
        )
        _install_rejecting_shadow(connection, "object_retention_status")
        claim = connection.execute(
            """
            SELECT lifecycle_state FROM public.gc_claim_object(
                'sha256', %s, 'gc-one', clock_timestamp(),
                clock_timestamp() + interval '5 minutes')
            """,
            (digest,),
        ).fetchone()
        assert claim == ("gc_claimed",)
        assert connection.execute(
            "SELECT public.gc_record_delete_failure('sha256',%s,'gc-one',clock_timestamp(),'retry')",
            (digest,),
        ).fetchone() == (True,)
        assert connection.execute(
            """
            SELECT count(*) FROM public.gc_claim_object(
                'sha256', %s, 'gc-two', clock_timestamp(),
                clock_timestamp() + interval '5 minutes')
            """,
            (digest,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT public.gc_complete_object_delete('sha256',%s,'gc-two',clock_timestamp())",
            (digest,),
        ).fetchone() == (True,)
        assert connection.execute(
            "SELECT count(*) FROM pg_temp.object_blob"
        ).fetchone() == (0,)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT lifecycle_state FROM object_blob WHERE digest_value=%s", (digest,)
        ).fetchone() == ("gc_deleted",)
