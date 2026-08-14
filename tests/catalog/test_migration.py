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

    assert "FOR UPDATE SKIP LOCKED" in postgres_sql.CLAIM_SQL
    for statement in (
        postgres_sql.HEARTBEAT_SQL,
        postgres_sql.COMPLETE_SQL,
        postgres_sql.FAIL_SQL,
    ):
        assert "lease_token = %(lease_token)s" in statement
        assert "lease_generation = %(lease_generation)s" in statement
        assert "lease_expires_utc > clock_timestamp()" in statement


def test_postgres_recording_read_joins_both_objects_without_update_lock() -> None:
    from leo_flow.storage import postgres_sql

    assert "JOIN object_blob AS data" in postgres_sql.GET_RECORDING_SQL
    assert "JOIN object_blob AS metadata" in postgres_sql.GET_RECORDING_SQL
    assert "register_live_object_blob" in postgres_sql.REGISTER_OBJECT_SQL
    assert "lifecycle_state = 'live'" in postgres_sql.VERIFY_OBJECT_SQL
    assert "FOR SHARE" not in postgres_sql.VERIFY_OBJECT_SQL
