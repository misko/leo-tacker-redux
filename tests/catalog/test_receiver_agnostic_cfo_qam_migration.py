from pathlib import Path


def test_0053_is_candidate_only_bounded_and_not_capture_work() -> None:
    sql = Path(
        "migrations/0053_recording_receiver_agnostic_cfo_qam_v0_6.sql"
    ).read_text()
    assert "stream_count BETWEEN 1 AND 2" in sql
    assert "window_count BETWEEN stream_count AND 6" in sql
    assert "CHECK(candidates_only)" in sql
    assert "JOIN public.object_blob data_object" in sql
    assert "JOIN public.object_blob metadata_object" in sql
    assert "data_object.lifecycle_state='live'" in sql
    assert "metadata_object.lifecycle_state='live'" in sql
    assert (
        "GRANT EXECUTE ON FUNCTION public.publish_recording_receiver_agnostic_cfo_qam_v0_6(jsonb)"
        in sql
    )
    assert " TO leo_analysis;" in sql
    assert "TO leo_capture" not in sql.replace("FROM PUBLIC,leo_capture", "")
    assert "queue" not in sql.lower()
