from pathlib import Path


def test_full_dwell_work_is_bounded_fenced_and_capture_independent() -> None:
    sql = Path("migrations/0042_starlink_full_dwell_work.sql").read_text()
    for required in (
        "pg_advisory_xact_lock",
        "$1 NOT BETWEEN 1 AND 64",
        "$2 NOT BETWEEN 1 AND 256",
        "FOR UPDATE SKIP LOCKED LIMIT 1",
        "lease_generation",
        "lease_expires_utc>clock_timestamp()",
        "result_analysis_id",
        "GRANT EXECUTE",
    ):
        assert required in sql
    assert "capture_analysis_inactive" not in sql
    assert "capture_analysis_drain_ready" not in sql
    assert "TO leo_capture" not in sql


def test_full_dwell_backfill_prioritizes_recent_unpublished_suites() -> None:
    sql = Path("migrations/0042_starlink_full_dwell_work.sql").read_text()
    assert "ORDER BY s.published_at_utc DESC,s.analysis_id DESC LIMIT capacity" in sql
    assert "NOT EXISTS(SELECT 1 FROM public.recording_starlink_full_dwell_v0_1" in sql
