from pathlib import Path


def test_adaptive_reanalysis_priority_prevents_continuous_arrival_starvation() -> None:
    sql = Path("migrations/0051_starlink_adaptive_response_priority.sql").read_text()

    assert "priority smallint NOT NULL DEFAULT 0" in sql
    assert "CHECK(priority BETWEEN 0 AND 100)" in sql
    assert "priority=100" in sql
    assert "last_error LIKE 'analysis-plan-%'" in sql
    assert "ORDER BY w.priority DESC,w.available_at_utc DESC" in sql
    assert "w.result_analysis_id=$2" in sql
    assert "DELETE" not in sql
    assert "UPDATE public.recording_starlink_adaptive_response_v0_1" not in sql
