from pathlib import Path


def test_full_dwell_catalog_is_immutable_live_closed_and_role_scoped() -> None:
    sql = Path("migrations/0041_starlink_full_dwell_response_v0_1.sql").read_text()
    assert "recording_starlink_full_dwell_v0_1.bundle" in sql
    assert "object_blob_assert_live_reference" in sql
    assert "full-dwell catalog identity conflict" in sql
    assert "jsonb_array_length(rows)>262144" in sql
    assert "LIMIT $6" in sql
    assert "TO leo_analysis;" in sql and "TO leo_dashboard;" in sql
    assert "TO leo_capture;" not in sql
    assert "UPDATE public.recording_starlink_full_dwell" not in sql
    assert "DELETE FROM public.recording_starlink_full_dwell" not in sql


def test_full_dwell_point_identity_and_scientific_coordinates_are_explicit() -> None:
    sql = Path("migrations/0041_starlink_full_dwell_response_v0_1.sql").read_text()
    for field in (
        "recording_id",
        "segment_id",
        "radio_id",
        "receiver_chain_id",
        "edge",
        "method",
        "window_index",
        "interval_start_utc_ns",
        "qin_score",
        "qin_winning_epoch_sample_in_segment",
        "qin_winning_coarse_cfo_hz",
        "qin_winning_residual_cfo_hz",
        "surrogate_scores",
        "finite_upper_tail_rank",
        "qin_minus_max_surrogate",
    ):
        assert field in sql
