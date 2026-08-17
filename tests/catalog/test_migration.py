from pathlib import Path


def test_first_slice_schema_has_atomic_pair_and_fenced_jobs() -> None:
    sql = Path("migrations/0001_first_slice.sql").read_text()
    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert "data_digest_value text NOT NULL" in sql
    assert "metadata_digest_value text NOT NULL" in sql
    assert "CHECK (data_digest_value <> metadata_digest_value)" in sql
    assert "lease_generation bigint NOT NULL" in sql
    assert "state = 'leased' AND lease_token IS NOT NULL" in sql


def test_postgres_claim_uses_skip_locked_and_all_mutations_are_fenced() -> None:
    from leo_flow.jobs import postgres_sql

    migration = Path("migrations/0015_job_parking.sql").read_text()
    assert "FOR UPDATE SKIP LOCKED" in migration
    assert "CREATE FUNCTION lock_active_job_lease" in migration
    for name, statement in (
        ("heartbeat_job", postgres_sql.HEARTBEAT_SQL),
        ("complete_job", postgres_sql.COMPLETE_SQL),
        ("fail_job", postgres_sql.FAIL_SQL),
        ("park_job", postgres_sql.PARK_SQL),
    ):
        assert name in statement
        assert "%(lease_token)s" in statement
        assert "%(lease_generation)s" in statement
    assert "lease_expires_utc > clock_timestamp()" in migration


def test_postgres_recording_read_joins_both_objects_without_update_lock() -> None:
    from leo_flow.storage import postgres_sql

    assert "JOIN object_blob AS data" in postgres_sql.GET_RECORDING_SQL
    assert "JOIN object_blob AS metadata" in postgres_sql.GET_RECORDING_SQL
    assert "register_live_object_blob" in postgres_sql.REGISTER_OBJECT_SQL
    assert "lifecycle_state = 'live'" in postgres_sql.VERIFY_OBJECT_SQL
    assert "FOR SHARE" not in postgres_sql.VERIFY_OBJECT_SQL


def test_dwell_ingress_reuses_fenced_job_state_and_preserves_route_and_expiry() -> None:
    from leo_flow.adapters import dwell_postgres_sql

    migration = Path("migrations/0019_dwell_request_ingress.sql").read_text()
    assert "job_id text NOT NULL UNIQUE REFERENCES public.job(job_id)" in migration
    assert "job_type = 'dwell_capture'" in migration
    assert "FOR UPDATE OF j SKIP LOCKED" in migration
    assert "d.station_id = p_station_id AND d.radio_id = p_radio_id" in migration
    assert "d.expires_utc_ns" in migration
    assert "lease_generation = j.lease_generation + 1" in migration
    for statement in (
        dwell_postgres_sql.HEARTBEAT_SQL,
        dwell_postgres_sql.COMPLETE_SQL,
        dwell_postgres_sql.FAIL_SQL,
        dwell_postgres_sql.PARK_SQL,
    ):
        assert "%(lease_token)s" in statement
        assert "%(lease_generation)s" in statement


def test_dwell_roles_cannot_bypass_directional_functions() -> None:
    migration = Path("migrations/0019_dwell_request_ingress.sql").read_text()
    assert "REVOKE ALL ON public.dwell_request_ingress" in migration
    assert "public.publish_dwell_request(jsonb) TO leo_analysis" in migration
    assert "public.claim_dwell_request(text, text, text, interval)" in migration
    assert "TO leo_capture;" in migration
    assert "GRANT SELECT ON public.feature_set TO leo_routine_owner" in migration


def test_starlink_detector_suite_migration_is_fenced_and_terminal_for_clipped_rates() -> (
    None
):
    migration = Path("migrations/0029_starlink_detector_suite_v0_2.sql").read_text()
    assert "starlink_suite_analysis" in migration
    assert "result_state IN ('candidates','not_evaluated')" in migration
    assert "method_count=suite_count*8" in migration
    assert "FOR UPDATE SKIP LOCKED" in migration
    assert "lease_generation=w.lease_generation+1" in migration
    assert "publish_dashboard_recording_starlink_detector_suite" in migration
    assert (
        "whole-search" not in migration
    )  # verdict/calibration policy stays in contracts
    assert "capture_analysis_drain_ready" in migration


def test_campaign_scoped_analysis_claims_are_bounded_fenced_and_private() -> None:
    migration = Path("migrations/0030_campaign_scoped_analysis_claims.sql").read_text()
    assert "pg_catalog.cardinality(p_job_ids) NOT BETWEEN 1 AND 72" in migration
    assert "j.job_id = ANY(p_job_ids)" in migration
    assert "AND j.job_type=p_job_type" in migration
    assert "j.job_type IN" not in migration
    assert "source_job_id=ANY(p_source_job_ids)" in migration
    assert migration.count("FOR UPDATE SKIP LOCKED") == 4
    assert migration.count("lease_generation") >= 8
    assert "TO leo_analysis;" in migration
    assert (
        "FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance"
        in migration
    )


def test_radio_lifecycle_migration_is_immutable_idempotent_and_private() -> None:
    migration = Path("migrations/0031_radio_lifecycle_detection.sql").read_text()
    assert "CREATE TABLE public.capture_attempt_radio_lifecycle_fact" in migration
    assert "CREATE TABLE public.radio_lifecycle_interval_fact" in migration
    assert "capture attempt lifecycle fact conflict" in migration
    assert "radio lifecycle interval fact conflict" in migration
    assert "read_latest_radio_lifecycle_terminal" in migration
    assert "read_capture_attempt_radio_lifecycle_fact" in migration
    assert "GRANT EXECUTE ON FUNCTION" in migration
    assert "TO leo_capture;" in migration
    assert "TO leo_dashboard;" in migration
    assert "GRANT SELECT" not in migration
    assert "UPDATE public.capture_attempt_radio_lifecycle_fact" not in migration
    assert "DELETE FROM public.capture_attempt_radio_lifecycle_fact" not in migration
    assert migration.count("OWNER TO leo_routine_owner;") == 6


def test_feature_projection_work_is_atomic_fenced_and_capability_scoped() -> None:
    from leo_flow.adapters import feature_projection_work_postgres_sql

    migration = Path("migrations/0020_feature_projection_work.sql").read_text()
    assert (
        "source_job_id text NOT NULL UNIQUE REFERENCES public.job(job_id)" in migration
    )
    assert "FOR UPDATE SKIP LOCKED" in migration
    assert "lease_generation = w.lease_generation + 1" in migration
    assert "job_type = 'recording_analysis'" in migration
    assert "REVOKE ALL ON public.feature_projection_work" in migration
    assert "TO leo_analysis;" in migration
    for statement in (
        feature_projection_work_postgres_sql.HEARTBEAT_SQL,
        feature_projection_work_postgres_sql.COMPLETE_SQL,
        feature_projection_work_postgres_sql.RETRY_SQL,
        feature_projection_work_postgres_sql.PARK_SQL,
    ):
        assert "%(lease_token)s" in statement
        assert "%(lease_generation)s" in statement


def test_analysis_migration_receipt_access_is_read_only_and_scoped() -> None:
    migration = Path("migrations/0022_analysis_migration_receipt_read.sql").read_text()
    assert "GRANT SELECT ON TABLE public.schema_migration TO leo_analysis;" in migration
    assert "leo_capture" not in migration
    assert "leo_dashboard" not in migration
    assert "INSERT" not in migration
    assert "UPDATE" not in migration
    assert "DELETE" not in migration


def test_campaign_projection_receipt_is_function_only_and_scoped() -> None:
    migration = Path("migrations/0023_campaign_projection_receipt.sql").read_text()
    assert "read_feature_projection_receipt" in migration
    assert "SECURITY DEFINER" in migration
    assert "TO leo_analysis;" in migration
    assert "TO leo_capture" not in migration
    assert "TO leo_dashboard" not in migration


def test_capture_analysis_inactive_gate_is_lease_only_and_capture_scoped() -> None:
    migration = Path("migrations/0024_capture_analysis_inactive.sql").read_text()
    assert "CREATE FUNCTION public.capture_analysis_inactive()" in migration
    assert "SECURITY DEFINER" in migration
    assert "SET search_path = pg_catalog, pg_temp" in migration
    assert "active_job.state = 'leased'" in migration
    assert "'recording_analysis', 'model_analysis'" in migration
    assert "active_projection.state = 'leased'" in migration
    assert "lease_expires_utc" in migration
    assert "TO leo_capture;" in migration
    assert "TO leo_analysis" not in migration
    assert "TO leo_dashboard" not in migration


def test_capture_analysis_waterfall_drain_is_full_and_capture_scoped() -> None:
    migration = Path("migrations/0027_capture_analysis_waterfall_drain.sql").read_text()
    assert (
        "CREATE OR REPLACE FUNCTION public.capture_analysis_drain_ready()" in migration
    )
    assert "SECURITY DEFINER" in migration
    assert "SET search_path = pg_catalog, pg_temp" in migration
    assert "'recording_analysis', 'waterfall_analysis'" in migration
    assert "pending_job.state IN ('ready', 'leased', 'failed')" in migration
    assert "FROM public.feature_projection_work AS pending_projection" in migration
    assert "FROM public.waterfall_projection_work AS pending_waterfall" in migration
    assert "pending_waterfall.state IN ('ready', 'leased', 'failed')" in migration
    assert "OWNER TO leo_routine_owner" in migration
    assert (
        "FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance"
        in migration
    )
    assert "TO leo_capture;" in migration
    assert "TO leo_analysis" not in migration
    assert "TO leo_dashboard" not in migration


def test_starlink_candidate_pipeline_is_fenced_projected_and_non_decisional() -> None:
    migration = Path(
        "migrations/0028_recording_starlink_candidate_pipeline.sql"
    ).read_text()
    assert "CREATE TABLE public.recording_starlink_candidate" in migration
    assert "CREATE TABLE public.starlink_projection_work" in migration
    assert "CREATE TABLE public.dashboard_recording_starlink_projection" in migration
    assert "j.job_type = 'starlink_analysis'" in migration
    assert "j.lease_token = p_source_lease_token" in migration
    assert "j.lease_generation = p_source_lease_generation" in migration
    assert "FOR UPDATE SKIP LOCKED" in migration
    assert "calibrated_detection_count}'<>'null'::jsonb" in migration
    assert "'starlink_analysis'" in migration
    assert "FROM public.starlink_projection_work AS w" in migration
    assert "SET search_path=pg_catalog,pg_temp" in migration
    assert "OWNER TO leo_routine_owner" in migration
    assert "TO leo_dashboard;" in migration
    assert "publish_dashboard_recording_starlink(jsonb,text,text,bigint)" in migration


def test_dashboard_batch_projection_is_versioned_and_source_independent() -> None:
    from leo_flow.adapters import dashboard_batch_postgres_sql

    migration = Path(
        "migrations/0021_dashboard_capture_batch_projection.sql"
    ).read_text()
    assert "CREATE TABLE public.dashboard_capture_batch_projection" in migration
    assert "CREATE TABLE public.dashboard_capture_attempt_projection" in migration
    assert "semantic_view jsonb NOT NULL" in migration
    assert "SECURITY DEFINER" in migration
    assert "SET search_path = pg_catalog, pg_temp" in migration
    assert "pg_advisory_xact_lock" in migration
    assert "available analysis result cannot regress" in migration
    assert "CREATE FUNCTION public.capture_analysis_drain_ready()" in migration
    assert "pending_job.job_type = 'recording_analysis'" in migration
    assert "latest_recording.analysis_state IN ('pending', 'running')" in migration
    assert "pending_projection.state IN ('ready', 'leased', 'failed')" in migration
    assert (
        "GRANT EXECUTE ON FUNCTION public.capture_analysis_drain_ready()" in migration
    )
    assert "TO leo_capture, leo_analysis;" in migration
    assert "TO leo_dashboard;" in migration
    combined_read_sql = (
        dashboard_batch_postgres_sql.BATCH_PROJECTION_ANCHOR_SQL
        + dashboard_batch_postgres_sql.RECENT_BATCHES_SQL
        + dashboard_batch_postgres_sql.EXACT_BATCH_SQL
        + dashboard_batch_postgres_sql.BATCH_ATTEMPTS_SQL
    ).lower()
    for forbidden in (
        "object_blob",
        "feature_projection_work",
        "dwell_request_ingress",
        "from public.recording",
        "join public.recording",
        "from public.job",
        "join public.job",
        "locator",
    ):
        assert forbidden not in combined_read_sql


def test_dashboard_waterfall_json_loops_remain_jsonb_and_parenthesize_extraction() -> (
    None
):
    migration = Path(
        "migrations/0026_dashboard_recording_detail_waterfall_projection.sql"
    ).read_text()
    for declaration in (
        "tile jsonb;",
        "power_row jsonb;",
        "power_value jsonb;",
    ):
        assert declaration in migration
    assert "tile_key := (tile->>'segment_id') || ':' ||" in migration
    assert "(tile->>'receiver_chain_id');" in migration
    assert "tile_key := tile->>'segment_id' ||" not in migration


def test_online_analysis_scope_is_terminal_exact_and_role_separated() -> None:
    migration = Path("migrations/0032_campaign_online_analysis.sql").read_text()
    assert "CREATE TABLE public.campaign_analysis_window_scope" in migration
    assert "cardinality(batch_ids) = 36" in migration
    assert "cardinality(recording_ids) = 72" in migration
    assert "j.payload ->> 'recording_id' <> expected.recording_id" in migration
    assert "CREATE FUNCTION public.capture_campaign_analysis_safe_v1" in migration
    assert "s.definition_digest=p_definition_digest" in migration
    assert "TO leo_analysis;" in migration
    assert "TO leo_capture;" in migration


def test_registered_analysis_gate_accepts_only_scoped_terminal_work() -> None:
    migration = Path(
        "migrations/0033_registered_analysis_during_capture.sql"
    ).read_text()
    assert "CREATE FUNCTION public.capture_registered_analysis_safe_v2" in migration
    assert "s.source_job_id=j.job_id" in migration
    assert "s.source_job_id=w.source_job_id" in migration
    assert "s.definition_digest=p_capture_definition_digest" not in migration
    assert "starlink_projection_work AS w" in migration
    assert "TO leo_capture;" in migration
    assert "TO leo_analysis;" not in migration
