from pathlib import Path


def test_adaptive_response_work_is_fenced_bounded_and_capture_independent() -> None:
    sql = Path("migrations/0046_starlink_adaptive_response_v0_1.sql").read_text()
    for required in (
        "FOR UPDATE OF w SKIP LOCKED LIMIT 1",
        "attempt<8",
        "lease_generation",
        "lease_expires_utc>clock_timestamp()",
        "recording_starlink_detector_suite",
        "recording_full_dwell_timeline_v0_1",
        "full_dwell_refinement_work_v0_1",
        "starlink_adaptive_response_work_v0_1",
        "recording_starlink_adaptive_response_v0_1",
        "result_state='candidates'",
        "o.lifecycle_state='live'",
    ):
        assert required in sql
    assert "TO leo_capture" not in sql


def test_adaptive_response_has_live_blob_closure_and_narrow_acl() -> None:
    sql = Path("migrations/0046_starlink_adaptive_response_v0_1.sql").read_text()
    assert "'recording_starlink_adaptive_response_v0_1.bundle'" in sql
    assert "TO leo_analysis" in sql
    assert "TO leo_dashboard" in sql
    assert "REVOKE ALL ON public.recording_starlink_adaptive_response_v0_1" in sql
