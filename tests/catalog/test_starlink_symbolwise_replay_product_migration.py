from pathlib import Path


def test_symbolwise_replay_migration_is_explicit_bounded_and_fenced() -> None:
    sql = Path(
        "migrations/0052_starlink_symbolwise_replay_product_v0_1.sql"
    ).read_text()

    assert "window_count=stream_count*600" in sql
    assert "pattern_evidence_count=window_count*5" in sql
    assert "CHECK(candidates_only)" in sql
    assert "explicit-on-demand-or-backfill" in sql
    assert "jsonb_array_length" in sql and "BETWEEN 1 AND 16" in sql
    assert "FOR UPDATE SKIP LOCKED LIMIT 1" in sql
    assert "w.state='leased'" in sql
    assert "w.lease_token=p->>'lease_token'" in sql
    assert "w.lease_generation=(p->>'lease_generation')::bigint" in sql
    assert "w.lease_expires_utc>clock_timestamp()" in sql
    assert "manifest_digest_value,state" in sql
    assert "GRANT EXECUTE ON FUNCTION" in sql
    capture_grants = tuple(
        line for line in sql.splitlines() if "TO leo_capture" in line
    )
    assert capture_grants == ()


def test_symbolwise_replay_migration_has_no_automatic_capture_or_backfill_path() -> (
    None
):
    sql = Path(
        "migrations/0052_starlink_symbolwise_replay_product_v0_1.sql"
    ).read_text()
    trigger_statements = tuple(
        line.strip() for line in sql.splitlines() if line.startswith("CREATE TRIGGER")
    )

    assert trigger_statements == (
        "CREATE TRIGGER recording_starlink_symbolwise_replay_bundle_live_v0_1",
    )
    assert "AFTER INSERT ON public.recording" not in sql
    assert "INSERT INTO public.starlink_symbolwise_replay_work_v0_1" in sql
    assert "INSERT INTO public.starlink_symbolwise_replay_work_v0_1 SELECT" not in sql
