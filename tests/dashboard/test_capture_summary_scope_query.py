from __future__ import annotations

from leo_flow.adapters.dashboard_capture_doppler_postgres import _QAM_SCOPE_SQL


def test_bounded_master_summary_scope_prioritizes_recent_recordings() -> None:
    normalized = " ".join(_QAM_SCOPE_SQL.split())
    assert (
        "ORDER BY has_published_qam DESC, observed_start_utc_ns DESC, recording_id"
    ) in normalized
    assert "read_latest_recording_starlink_adaptive_qam_v0_4" in normalized
    assert "read_latest_recording_starlink_acquired_constellation_v0_3" in normalized
