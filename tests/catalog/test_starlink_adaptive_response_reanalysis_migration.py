from pathlib import Path


def test_adaptive_reanalysis_is_exact_cas_and_preserves_old_products() -> None:
    sql = Path("migrations/0050_starlink_adaptive_response_reanalysis.sql").read_text()

    assert "w.recording_id=$1" in sql
    assert "w.result_analysis_id=$2" in sql
    assert "w.state='succeeded'" in sql
    assert "state='ready'" in sql
    assert "result_analysis_id=NULL" in sql
    assert "DELETE" not in sql
    assert "UPDATE public.recording_starlink_adaptive_response_v0_1" not in sql
    assert "TO leo_analysis" in sql
    assert "TO leo_capture" not in sql
    assert "TO leo_dashboard" not in sql
