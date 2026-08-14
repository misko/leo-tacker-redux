from __future__ import annotations

import threading
import time

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.contracts.core import Digest, DigestAlgorithm, RecordingId
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.maintenance.postgres_gc import PostgresGarbageCollectionCatalog
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog

_DIGEST = "a" * 64


def _insert_object(connection: psycopg.Connection, digest: str = _DIGEST) -> None:
    connection.execute(
        """
        INSERT INTO object_blob
            (digest_algorithm, digest_value, byte_count, media_type, format_id, locator)
        VALUES ('sha256', %s, 7, 'application/octet-stream', 'test-v1', %s)
        """,
        (digest, f"cas:sha256:{digest}"),
    )


def _make_collectible(connection: psycopg.Connection, digest: str = _DIGEST) -> None:
    connection.execute(
        """
        INSERT INTO object_retention_policy
            (policy_id, retain_for_seconds, grace_period_seconds,
             allow_remote_delete, rationale)
        VALUES ('test-expire', 0, 1, true, 'isolated integration fixture')
        """
    )
    connection.execute(
        """
        INSERT INTO object_retention_assignment
            (digest_algorithm, digest_value, policy_id, assigned_at, assigned_by)
        VALUES ('sha256', %s, 'test-expire', clock_timestamp() - interval '2 seconds',
                'integration-test')
        """,
        (digest,),
    )


def _catalog(postgres_dsn: str) -> PostgresGarbageCollectionCatalog:
    return PostgresGarbageCollectionCatalog(
        lambda: psycopg.connect(postgres_dsn, row_factory=dict_row)
    )


def test_reference_inventory_matches_every_current_object_blob_fk(
    postgres_dsn: str,
) -> None:
    expected = {
        ("recording", ("data_digest_algorithm", "data_digest_value")),
        ("recording", ("metadata_digest_algorithm", "metadata_digest_value")),
        ("ephemeris_snapshot", ("raw_digest_algorithm", "raw_digest_value")),
        (
            "ephemeris_snapshot",
            ("normalized_digest_algorithm", "normalized_digest_value"),
        ),
        (
            "ephemeris_snapshot",
            ("provenance_digest_algorithm", "provenance_digest_value"),
        ),
        ("dataset_snapshot", ("bundle_digest_algorithm", "bundle_digest_value")),
        ("feature_set", ("bundle_digest_algorithm", "bundle_digest_value")),
        ("model_snapshot", ("bundle_digest_algorithm", "bundle_digest_value")),
        (
            "tracking_input_snapshot",
            ("bundle_digest_algorithm", "bundle_digest_value"),
        ),
        ("hardware_snapshot", ("bundle_digest_algorithm", "bundle_digest_value")),
        (
            "detector_evaluation_report",
            ("report_digest_algorithm", "report_digest_value"),
        ),
        ("object_retention_assignment", ("digest_algorithm", "digest_value")),
    }
    with psycopg.connect(postgres_dsn) as connection:
        rows = connection.execute(
            """
            SELECT c.conrelid::regclass::text,
                   ARRAY(SELECT a.attname
                           FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                           JOIN pg_attribute a
                             ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                          ORDER BY k.ord)
              FROM pg_constraint c
             WHERE c.contype = 'f' AND c.confrelid = 'object_blob'::regclass
            """
        ).fetchall()
        actual = {(table, tuple(columns)) for table, columns in rows}
        assert actual == expected
        trigger_count = connection.execute(
            """
            SELECT count(*) FROM pg_trigger
             WHERE tgfoid = 'object_blob_assert_live_reference()'::regprocedure
               AND NOT tgisinternal
            """
        ).fetchone()[0]
        assert trigger_count == len(expected) - 1


def test_explicit_policy_and_zero_references_are_required(postgres_dsn: str) -> None:
    now = time.time_ns()
    with psycopg.connect(postgres_dsn) as connection:
        _insert_object(connection)
    catalog = _catalog(postgres_dsn)
    assert catalog.candidates(as_of_utc_ns=now + 10_000_000_000, limit=10) == ()

    with psycopg.connect(postgres_dsn) as connection:
        _make_collectible(connection)
    candidates = catalog.candidates(as_of_utc_ns=now + 10_000_000_000, limit=10)
    assert len(candidates) == 1

    claimed = catalog.claim(
        candidates[0],
        claim_token="claim-one",
        claimed_at_utc_ns=now + 10_000_000_000,
        claim_expires_at_utc_ns=now + 20_000_000_000,
    )
    assert claimed == candidates[0]
    assert catalog.complete(
        claimed, claim_token="claim-one", completed_at_utc_ns=now + 11_000_000_000
    )
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT lifecycle_state FROM object_blob WHERE digest_value = %s",
            (_DIGEST,),
        ).fetchone() == ("gc_deleted",)
        assert connection.execute(
            "SELECT event FROM object_gc_attempt ORDER BY attempt_id"
        ).fetchall() == [("claimed",), ("deleted",)]

    # Republishing exact bytes after the remote writer has recreated them
    # deliberately resurrects the tombstone in the same transaction that adds
    # the new live reference. A mismatched identity cannot use this path.
    data = ObjectRef(
        Digest(DigestAlgorithm.SHA256, _DIGEST),
        7,
        "application/octet-stream",
        "test-v1",
        f"cas:sha256:{_DIGEST}",
    )
    metadata_digest = "b" * 64
    metadata = ObjectRef(
        Digest(DigestAlgorithm.SHA256, metadata_digest),
        7,
        "application/octet-stream",
        "test-v1",
        f"cas:sha256:{metadata_digest}",
    )
    with psycopg.connect(postgres_dsn) as connection:
        _insert_object(connection, metadata_digest)
    recording = RecordingObjectRef(
        RecordingId("rec_republish"), data, metadata, Digest.sha256(b"manifest")
    )
    published = PostgresRecordingCatalog(
        lambda: psycopg.connect(postgres_dsn, row_factory=dict_row)
    ).publish(recording, idempotency_key="republish-after-gc")
    assert published.recording_object == recording
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT lifecycle_state FROM object_blob WHERE digest_value = %s",
            (_DIGEST,),
        ).fetchone() == ("live",)


def test_live_reference_wins_race_with_claim(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as setup:
        _insert_object(setup)
        _make_collectible(setup)

    inserted = threading.Event()
    release = threading.Event()

    def publish_reference() -> None:
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                INSERT INTO recording
                    (recording_id, data_digest_value, metadata_digest_value,
                     manifest_digest_value, idempotency_key, state)
                VALUES ('rec_race', %s, %s, %s, 'race', 'published')
                """,
                (_DIGEST, "b" * 64, "c" * 64),
            )
            inserted.set()
            assert release.wait(5)

    with psycopg.connect(postgres_dsn) as setup:
        _insert_object(setup, "b" * 64)
    thread = threading.Thread(target=publish_reference)
    thread.start()
    assert inserted.wait(5)
    catalog = _catalog(postgres_dsn)
    candidate = catalog.candidates(as_of_utc_ns=time.time_ns(), limit=1)[0]

    timer = threading.Timer(0.2, release.set)
    timer.start()
    claimed = catalog.claim(
        candidate,
        claim_token="losing-claim",
        claimed_at_utc_ns=time.time_ns(),
        claim_expires_at_utc_ns=time.time_ns() + 10_000_000_000,
    )
    thread.join(5)
    timer.cancel()
    assert claimed is None


def test_claimed_tombstone_rejects_new_reference(postgres_dsn: str) -> None:
    now = time.time_ns()
    with psycopg.connect(postgres_dsn) as connection:
        _insert_object(connection)
        _insert_object(connection, "b" * 64)
        _make_collectible(connection)
    catalog = _catalog(postgres_dsn)
    candidate = catalog.candidates(as_of_utc_ns=now + 10_000_000_000, limit=1)[0]
    assert catalog.claim(
        candidate,
        claim_token="fenced",
        claimed_at_utc_ns=now + 10_000_000_000,
        claim_expires_at_utc_ns=now + 20_000_000_000,
    )

    with (
        psycopg.connect(postgres_dsn) as connection,
        pytest.raises(psycopg.errors.ForeignKeyViolation),
    ):
        connection.execute(
            """
                INSERT INTO recording
                    (recording_id, data_digest_value, metadata_digest_value,
                     manifest_digest_value, idempotency_key, state)
                VALUES ('rec_too_late', %s, %s, %s, 'late', 'published')
                """,
            (_DIGEST, "b" * 64, "c" * 64),
        )


def test_roles_cannot_delete_or_claim_outside_maintenance(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        for role in ("leo_capture", "leo_analysis", "leo_dashboard"):
            assert not connection.execute(
                "SELECT has_table_privilege(%s, 'object_blob', 'DELETE')", (role,)
            ).fetchone()[0]
            assert not connection.execute(
                """
                SELECT has_function_privilege(
                    %s,
                    'gc_claim_object(text,text,text,timestamptz,timestamptz)',
                    'EXECUTE')
                """,
                (role,),
            ).fetchone()[0]
        assert not connection.execute(
            "SELECT has_table_privilege('leo_maintenance', 'object_blob', 'DELETE')"
        ).fetchone()[0]
        assert connection.execute(
            """
            SELECT has_function_privilege(
                'leo_maintenance',
                'gc_claim_object(text,text,text,timestamptz,timestamptz)',
                'EXECUTE')
            """
        ).fetchone()[0]
