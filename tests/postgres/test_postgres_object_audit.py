"""Restore-drill audit against a real PostgreSQL catalog."""

from __future__ import annotations

import io

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.contracts.core import Digest
from leo_flow.maintenance import audit_objects
from leo_flow.maintenance.postgres_objects import PostgresObjectInventory
from leo_flow.storage import FileSystemBlobStore


def _connect(postgres_dsn: str):
    return lambda: psycopg.connect(postgres_dsn, row_factory=dict_row)


def _register(postgres_dsn: str, ref) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            INSERT INTO object_blob
                (digest_algorithm, digest_value, byte_count,
                 media_type, format_id, locator, verified_at)
            VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp())
            """,
            (
                ref.digest.algorithm.value,
                ref.digest.value,
                ref.byte_count,
                ref.media_type,
                ref.format_id,
                ref.locator,
            ),
        )


@pytest.mark.integration
def test_restored_catalog_inventory_verifies_exact_cas_bytes(
    postgres_dsn: str, tmp_path
) -> None:
    blobs = FileSystemBlobStore(tmp_path / "cas")
    payload = b"restored authoritative object"
    ref = blobs.put(
        io.BytesIO(payload),
        expected_digest=Digest.sha256(payload),
        expected_bytes=len(payload),
        media_type="application/octet-stream",
        format_id="restore-audit-v1",
        idempotency_key="restore-audit",
    )
    _register(postgres_dsn, ref)

    report = audit_objects(PostgresObjectInventory(_connect(postgres_dsn)), blobs)

    assert report.passed
    assert (report.object_count, report.verified_count) == (1, 1)


@pytest.mark.integration
def test_restored_catalog_audit_reports_missing_blob_without_db_mutation(
    postgres_dsn: str, tmp_path
) -> None:
    blobs = FileSystemBlobStore(tmp_path / "cas")
    payload = b"missing after restore"
    ref = blobs.put(
        io.BytesIO(payload),
        expected_digest=Digest.sha256(payload),
        expected_bytes=len(payload),
        media_type="application/octet-stream",
        format_id="restore-audit-v1",
        idempotency_key="restore-missing",
    )
    _register(postgres_dsn, ref)
    path = tmp_path / "cas" / "sha256" / ref.digest.value[:2] / ref.digest.value
    path.unlink()

    report = audit_objects(PostgresObjectInventory(_connect(postgres_dsn)), blobs)

    assert not report.passed
    assert report.failures[0].object_digest == str(ref.digest)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM object_blob").fetchone() == (1,)
