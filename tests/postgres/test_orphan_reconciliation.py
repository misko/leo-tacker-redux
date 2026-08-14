from __future__ import annotations

import io
import threading
import time
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.contracts.core import Digest
from leo_flow.maintenance.filesystem_orphans import (
    FileSystemCasInventory,
    MaintenanceOrphanFileDeleter,
)
from leo_flow.maintenance.postgres_orphans import (
    PostgresOrphanReconciliationCatalog,
)
from leo_flow.storage import FileSystemBlobStore


def _factory(dsn: str):
    return lambda: psycopg.connect(dsn, row_factory=dict_row)


def _entry(root: Path, payload: bytes = b"unregistered"):
    digest = Digest.sha256(payload)
    FileSystemBlobStore(root).put(
        io.BytesIO(payload),
        expected_digest=digest,
        expected_bytes=len(payload),
        media_type="application/octet-stream",
        format_id="test-v1",
        idempotency_key=digest.value,
    )
    inventory = FileSystemCasInventory(root)
    return inventory, inventory.inventory(after=None, limit=1).entries[0]


def _age_observation(dsn: str, digest: str) -> None:
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            UPDATE object_orphan_observation
               SET first_observed_at = clock_timestamp() - interval '2 days'
             WHERE digest_value = %s
            """,
            (digest,),
        )


def _register(connection, entry) -> None:
    assert entry.digest is not None and entry.evidence is not None
    connection.execute(
        "SELECT register_live_object_blob(%s, %s, %s, %s, %s, %s)",
        (
            "sha256",
            entry.digest.value,
            entry.evidence.byte_count,
            "application/octet-stream",
            "test-v1",
            entry.locator,
        ),
    )


def test_observation_classifies_live_registered_tombstone_and_in_flight(
    postgres_dsn: str, tmp_path: Path
) -> None:
    _, entry = _entry(tmp_path)
    catalog = PostgresOrphanReconciliationCatalog(_factory(postgres_dsn))
    assert catalog.observe(entry).value == "unregistered"
    _age_observation(postgres_dsn, entry.digest.value)  # type: ignore[union-attr]
    token = catalog.claim(entry, minimum_age_seconds=1)
    assert token is not None
    assert catalog.observe(entry).value == "in_flight"
    assert catalog.pending_claims(limit=1)[0].claim_token == token

    with (
        psycopg.connect(postgres_dsn) as connection,
        pytest.raises(psycopg.errors.SerializationFailure),
    ):
        _register(connection, entry)


def test_direct_registration_preserves_conflict_for_adapter_verification(
    postgres_dsn: str, tmp_path: Path
) -> None:
    _, entry = _entry(tmp_path)
    with psycopg.connect(postgres_dsn) as connection:
        _register(connection, entry)
    assert entry.digest is not None and entry.evidence is not None
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            "SELECT register_live_object_blob(%s, %s, %s, %s, %s, %s)",
            (
                "sha256",
                entry.digest.value,
                entry.evidence.byte_count + 1,
                "application/octet-stream",
                "test-v1",
                entry.locator,
            ),
        )
        assert connection.execute(
            "SELECT byte_count FROM object_blob WHERE digest_value = %s",
            (entry.digest.value,),
        ).fetchone() == (entry.evidence.byte_count,)


def test_registration_first_serializes_and_prevents_orphan_claim(
    postgres_dsn: str, tmp_path: Path
) -> None:
    _, entry = _entry(tmp_path)
    catalog = PostgresOrphanReconciliationCatalog(_factory(postgres_dsn))
    assert catalog.observe(entry).value == "unregistered"
    _age_observation(postgres_dsn, entry.digest.value)  # type: ignore[union-attr]
    registered = threading.Event()
    release = threading.Event()

    def hold_registration() -> None:
        with psycopg.connect(postgres_dsn) as connection:
            _register(connection, entry)
            registered.set()
            assert release.wait(5)

    thread = threading.Thread(target=hold_registration)
    thread.start()
    assert registered.wait(5)
    timer = threading.Timer(0.2, release.set)
    timer.start()
    assert catalog.claim(entry, minimum_age_seconds=1) is None
    thread.join(5)
    timer.cancel()
    assert not thread.is_alive()


def test_unlink_holds_digest_fence_until_completion_then_registration_survives(
    postgres_dsn: str, tmp_path: Path
) -> None:
    payload = b"publication-race"
    inventory, entry = _entry(tmp_path, payload)
    assert entry.digest is not None
    catalog = PostgresOrphanReconciliationCatalog(_factory(postgres_dsn))
    assert catalog.observe(entry).value == "unregistered"
    _age_observation(postgres_dsn, entry.digest.value)
    token = catalog.claim(entry, minimum_age_seconds=1)
    assert token is not None
    unlinked = threading.Event()
    release = threading.Event()
    outcome: list[str] = []

    def delete_and_hold() -> None:
        def action() -> None:
            MaintenanceOrphanFileDeleter(inventory).delete_exact(entry)
            unlinked.set()
            assert release.wait(5)

        outcome.append(
            catalog.delete_under_fence(entry, claim_token=token, delete=action)
        )

    collector = threading.Thread(target=delete_and_hold)
    collector.start()
    assert unlinked.wait(5)

    publisher_done = threading.Event()

    def publish() -> None:
        FileSystemBlobStore(tmp_path).put(
            io.BytesIO(payload),
            expected_digest=entry.digest,
            expected_bytes=len(payload),
            media_type="application/octet-stream",
            format_id="test-v1",
            idempotency_key="race-republish",
        )
        with psycopg.connect(postgres_dsn) as connection:
            _register(connection, entry)
        publisher_done.set()

    publisher = threading.Thread(target=publish)
    publisher.start()
    time.sleep(0.2)
    assert not publisher_done.is_set(), "registration must wait for deletion fence"
    release.set()
    collector.join(5)
    publisher.join(5)
    assert outcome == ["deleted"]
    assert publisher_done.is_set()
    assert inventory.exact(inventory.inventory(after=None, limit=1).entries[0])
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT lifecycle_state FROM object_blob WHERE digest_value = %s",
            (entry.digest.value,),
        ).fetchone() == ("live",)


def test_crash_after_unlink_resumes_persistent_claim_idempotently(
    postgres_dsn: str, tmp_path: Path
) -> None:
    inventory, entry = _entry(tmp_path, b"restart")
    assert entry.digest is not None
    catalog = PostgresOrphanReconciliationCatalog(_factory(postgres_dsn))
    catalog.observe(entry)
    _age_observation(postgres_dsn, entry.digest.value)
    token = catalog.claim(entry, minimum_age_seconds=1)
    assert token is not None
    MaintenanceOrphanFileDeleter(inventory).delete_exact(entry)

    pending = catalog.pending_claims(limit=10)
    assert pending == (pending[0],)
    assert pending[0].claim_token == token
    assert (
        catalog.delete_under_fence(
            pending[0].entry,
            claim_token=token,
            delete=lambda: MaintenanceOrphanFileDeleter(inventory).delete_exact(
                pending[0].entry
            ),
        )
        == "deleted"
    )
    assert catalog.pending_claims(limit=10) == ()
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT event FROM object_orphan_event ORDER BY event_id"
        ).fetchall() == [("observed",), ("claimed",), ("deleted",)]


def test_changed_filesystem_evidence_is_never_unlinked(
    postgres_dsn: str, tmp_path: Path
) -> None:
    inventory, entry = _entry(tmp_path, b"old")
    assert entry.digest is not None
    catalog = PostgresOrphanReconciliationCatalog(_factory(postgres_dsn))
    catalog.observe(entry)
    _age_observation(postgres_dsn, entry.digest.value)
    token = catalog.claim(entry, minimum_age_seconds=1)
    assert token is not None
    path = tmp_path / entry.key
    path.unlink()
    path.write_bytes(b"new")

    assert (
        catalog.delete_under_fence(
            entry,
            claim_token=token,
            delete=lambda: MaintenanceOrphanFileDeleter(inventory).delete_exact(entry),
        )
        == "delete_failed"
    )
    assert path.read_bytes() == b"new"
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT state FROM object_orphan_observation WHERE digest_value = %s",
            (entry.digest.value,),
        ).fetchone() == ("claimed",)
        assert connection.execute(
            "SELECT detail FROM object_orphan_event WHERE event = 'delete_failed'"
        ).fetchone() == ("orphan-delete:EvidenceChangedError",)


def test_only_security_definer_functions_can_mutate_orphan_lifecycle(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        for role in ("leo_capture", "leo_analysis", "leo_dashboard"):
            assert not connection.execute(
                "SELECT has_table_privilege(%s, 'object_blob', 'INSERT')", (role,)
            ).fetchone()[0]
            assert not connection.execute(
                "SELECT has_table_privilege(%s, 'object_orphan_observation', 'INSERT')",
                (role,),
            ).fetchone()[0]
            assert not connection.execute(
                """
                SELECT has_function_privilege(
                    %s,
                    'claim_unregistered_object(text,text,bigint,text,bigint,bigint,bigint,bigint,bigint,text,bigint)',
                    'EXECUTE')
                """,
                (role,),
            ).fetchone()[0]
        assert connection.execute(
            """
            SELECT has_function_privilege(
                'leo_maintenance',
                'claim_unregistered_object(text,text,bigint,text,bigint,bigint,bigint,bigint,bigint,text,bigint)',
                'EXECUTE')
            """
        ).fetchone()[0]
