from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.hardware_link_postgres import (
    PostgresRecordingHardwareLinkCatalog,
    RecordingHardwareAuthorityMismatchError,
    RecordingHardwareLinkConflictError,
)
from leo_flow.adapters.hardware_postgres_catalog import (
    PostgresHardwareSnapshotCatalog,
    connection_factory,
)
from leo_flow.contracts.core import Digest, canonical_digest
from leo_flow.contracts.hardware import RecordingHardwareLink
from leo_flow.hardware import DurableHardwareMetadataRepository
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
from testkit import recording_object_ref
from tests.hardware.test_hardware_persistence import _snapshot


def _role_catalog(postgres_dsn: str):
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        connection.execute("SET ROLE leo_analysis")
        return connection

    return PostgresRecordingHardwareLinkCatalog(connect)


def _authorities(postgres_dsn: str, tmp_path):
    recording = recording_object_ref()
    published = PostgresRecordingCatalog(connection_factory(postgres_dsn)).publish(
        recording, idempotency_key="hardware-link-recording"
    )
    hardware = DurableHardwareMetadataRepository(
        FileSystemBlobStore(tmp_path / "cas"),
        PostgresHardwareSnapshotCatalog(connection_factory(postgres_dsn)),
    )
    hardware_ref = hardware.publish(
        _snapshot(), idempotency_key="hardware-link-snapshot"
    )
    identity = {
        "recording_id": str(recording.recording_id),
        "recording_identity_digest": str(recording.identity_digest()),
        "hardware_snapshot_id": str(hardware_ref.snapshot_id),
        "hardware_snapshot_digest": str(hardware_ref.digest),
    }
    digest = canonical_digest(identity)
    link = RecordingHardwareLink(
        f"hwlink_{digest.value[:32]}",
        recording.recording_id,
        recording.identity_digest(),
        hardware_ref,
        digest,
    )
    return published, hardware_ref, link


@pytest.mark.integration
def test_analysis_role_publishes_and_reads_exact_link(
    postgres_dsn: str, tmp_path
) -> None:
    _, _, link = _authorities(postgres_dsn, tmp_path)
    catalog = _role_catalog(postgres_dsn)

    assert catalog.publish(link, idempotency_key="recording-hardware:one") == link
    assert catalog.publish(link, idempotency_key="recording-hardware:one") == link
    assert catalog.get(link.recording_id) == link


@pytest.mark.integration
def test_link_rejects_non_authoritative_recording_identity(
    postgres_dsn: str, tmp_path
) -> None:
    _, _, link = _authorities(postgres_dsn, tmp_path)
    wrong = replace(link, recording_identity_digest=Digest.sha256(b"wrong"))
    with pytest.raises(RecordingHardwareAuthorityMismatchError):
        _role_catalog(postgres_dsn).publish(
            wrong, idempotency_key="recording-hardware:wrong-recording"
        )


@pytest.mark.integration
def test_exact_hardware_foreign_key_rejects_wrong_digest(
    postgres_dsn: str, tmp_path
) -> None:
    _, _, link = _authorities(postgres_dsn, tmp_path)
    parameters = (
        link.link_id,
        str(link.recording_id),
        link.recording_identity_digest.value,
        str(link.hardware_snapshot_ref.snapshot_id),
        Digest.sha256(b"wrong").value,
        link.link_digest.value,
        "direct-wrong-ref",
    )
    with (
        psycopg.connect(postgres_dsn) as connection,
        pytest.raises(psycopg.errors.ForeignKeyViolation),
    ):
        connection.execute(
            """
            INSERT INTO recording_hardware_link
                (link_id, recording_id, recording_identity_digest_algorithm,
                 recording_identity_digest_value, hardware_snapshot_id,
                 hardware_snapshot_digest_algorithm,
                 hardware_snapshot_digest_value, link_digest_algorithm,
                 link_digest_value, idempotency_key)
            VALUES (%s, %s, 'sha256', %s, %s, 'sha256', %s, 'sha256', %s, %s)
            """,
            parameters,
        )


@pytest.mark.integration
def test_one_recording_cannot_be_relinked(postgres_dsn: str, tmp_path) -> None:
    _, _, link = _authorities(postgres_dsn, tmp_path)
    catalog = _role_catalog(postgres_dsn)
    catalog.publish(link, idempotency_key="recording-hardware:stable")
    other_digest = Digest.sha256(b"different-link")
    conflicting = replace(
        link,
        link_id=f"hwlink_{other_digest.value[:32]}",
        link_digest=other_digest,
    )
    with pytest.raises(RecordingHardwareLinkConflictError):
        catalog.publish(conflicting, idempotency_key="recording-hardware:different")


@pytest.mark.integration
def test_concurrent_exact_link_publication_is_single_row(
    postgres_dsn: str, tmp_path
) -> None:
    _, _, link = _authorities(postgres_dsn, tmp_path)

    def publish(_index: int):
        return _role_catalog(postgres_dsn).publish(
            link, idempotency_key="recording-hardware:concurrent"
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = tuple(executor.map(publish, range(6)))
    assert results == (link,) * 6
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM recording_hardware_link"
        ).fetchone() == (1,)


@pytest.mark.integration
def test_existing_recording_remains_explicitly_unlinked(
    postgres_dsn: str, tmp_path
) -> None:
    published, _, _ = _authorities(postgres_dsn, tmp_path)
    assert _role_catalog(postgres_dsn).get(published.recording_id) is None
