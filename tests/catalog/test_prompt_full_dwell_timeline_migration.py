from pathlib import Path


def test_prompt_timeline_work_is_bounded_fenced_and_capture_independent() -> None:
    sql = Path("migrations/0044_prompt_full_dwell_timeline.sql").read_text()
    for required in (
        "FOR UPDATE SKIP LOCKED LIMIT 1",
        "pg_advisory_xact_lock(1186462802)",
        ">=8 THEN RETURN false",
        "lease_generation",
        "lease_expires_utc>clock_timestamp()",
        "attempt<8",
        "jsonb_array_length(q->'streams') NOT BETWEEN 1 AND 16",
        "ORDER BY r.published_at DESC,r.recording_id DESC LIMIT least($1,capacity)",
        "recording_hardware_link",
        "hardware_receiver_chain",
        "lifecycle_state='live'",
        "full_dwell_refinement_work_v0_1",
    ):
        assert required in sql
    assert "capture_analysis_inactive" not in sql
    assert "capture_analysis_drain_ready" not in sql
    assert "TO leo_capture" not in sql


def test_prompt_timeline_is_in_object_liveness_inventory() -> None:
    sql = Path("migrations/0044_prompt_full_dwell_timeline.sql").read_text()
    assert "'recording_full_dwell_timeline_v0_1.bundle'" in sql


def test_prompt_timeline_owner_has_only_declared_recording_source_columns() -> None:
    sql = Path("migrations/0045_prompt_timeline_source_acl.sql").read_text()
    for column in (
        "recording_id",
        "data_digest_algorithm",
        "data_digest_value",
        "metadata_digest_algorithm",
        "metadata_digest_value",
        "manifest_digest_value",
        "published_at",
    ):
        assert column in sql
    assert "ON public.recording TO leo_routine_owner" in sql
    assert "GRANT SELECT ON public.recording" not in sql
