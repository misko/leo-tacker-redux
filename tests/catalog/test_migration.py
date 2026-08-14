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

    migration = Path("migrations/0015_job_parking.sql").read_text()
    assert "FOR UPDATE SKIP LOCKED" in migration
    assert "CREATE FUNCTION lock_active_job_lease" in migration
    for name, statement in (
        ("heartbeat_job", postgres_sql.HEARTBEAT_SQL),
        ("complete_job", postgres_sql.COMPLETE_SQL),
        ("fail_job", postgres_sql.FAIL_SQL),
        ("park_job", postgres_sql.PARK_SQL),
    ):
        assert name in statement
        assert "%(lease_token)s" in statement
        assert "%(lease_generation)s" in statement
    assert "lease_expires_utc > clock_timestamp()" in migration


def test_postgres_recording_read_joins_both_objects_without_update_lock() -> None:
    from leo_flow.storage import postgres_sql

    assert "JOIN object_blob AS data" in postgres_sql.GET_RECORDING_SQL
    assert "JOIN object_blob AS metadata" in postgres_sql.GET_RECORDING_SQL
    assert "register_live_object_blob" in postgres_sql.REGISTER_OBJECT_SQL
    assert "lifecycle_state = 'live'" in postgres_sql.VERIFY_OBJECT_SQL
    assert "FOR SHARE" not in postgres_sql.VERIFY_OBJECT_SQL
