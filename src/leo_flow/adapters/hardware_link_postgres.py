"""Role-safe immutable recording-to-hardware link catalog."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from leo_flow.contracts.core import (
    Digest,
    DigestAlgorithm,
    HardwareSnapshotId,
    RecordingId,
)
from leo_flow.contracts.hardware import (
    HardwareMetadataSnapshotRef,
    RecordingHardwareLink,
)
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class RecordingHardwareLinkPersistenceError(RuntimeError):
    pass


class RecordingHardwareAuthorityMismatchError(RecordingHardwareLinkPersistenceError):
    pass


class RecordingHardwareLinkConflictError(RecordingHardwareLinkPersistenceError):
    pass


class PostgresRecordingHardwareLinkCatalog:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish(
        self, link: RecordingHardwareLink, *, idempotency_key: str
    ) -> RecordingHardwareLink:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        parameters = _parameters(link, idempotency_key)
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            authoritative = PostgresRecordingCatalog.get_with_cursor(
                cursor, str(link.recording_id)
            )
            if (
                authoritative is None
                or authoritative.recording_object.identity_digest()
                != link.recording_identity_digest
            ):
                raise RecordingHardwareAuthorityMismatchError(
                    "recording link does not match authoritative recording identity"
                )
            cursor.execute(_PUBLISH_SQL, parameters)
            if cursor.fetchone() is not None:
                return link
            cursor.execute(_CONFLICT_SQL, parameters)
            rows = cursor.fetchall()
            if len(rows) != 1:
                raise RecordingHardwareLinkConflictError(
                    "hardware link identities identify different rows"
                )
            existing = _link(rows[0])
            if existing != link or rows[0]["idempotency_key"] != idempotency_key:
                raise RecordingHardwareLinkConflictError(
                    "hardware link identity or idempotency key was reused"
                )
            return existing

    def get(self, recording_id: RecordingId) -> RecordingHardwareLink | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(_GET_SQL, {"recording_id": str(recording_id)})
            row = cursor.fetchone()
            return None if row is None else _link(row)


def _parameters(link: RecordingHardwareLink, idempotency_key: str) -> dict[str, object]:
    return {
        "link_id": link.link_id,
        "recording_id": str(link.recording_id),
        "recording_identity_digest_algorithm": (
            link.recording_identity_digest.algorithm.value
        ),
        "recording_identity_digest_value": link.recording_identity_digest.value,
        "hardware_snapshot_id": str(link.hardware_snapshot_ref.snapshot_id),
        "hardware_snapshot_digest_algorithm": (
            link.hardware_snapshot_ref.digest.algorithm.value
        ),
        "hardware_snapshot_digest_value": link.hardware_snapshot_ref.digest.value,
        "link_digest_algorithm": link.link_digest.algorithm.value,
        "link_digest_value": link.link_digest.value,
        "idempotency_key": idempotency_key,
    }


def _link(row: dict[str, object]) -> RecordingHardwareLink:
    return RecordingHardwareLink(
        str(row["link_id"]),
        RecordingId(str(row["recording_id"])),
        Digest(
            DigestAlgorithm(str(row["recording_identity_digest_algorithm"])),
            str(row["recording_identity_digest_value"]),
        ),
        HardwareMetadataSnapshotRef(
            HardwareSnapshotId(str(row["hardware_snapshot_id"])),
            Digest(
                DigestAlgorithm(str(row["hardware_snapshot_digest_algorithm"])),
                str(row["hardware_snapshot_digest_value"]),
            ),
        ),
        Digest(
            DigestAlgorithm(str(row["link_digest_algorithm"])),
            str(row["link_digest_value"]),
        ),
    )


_COLUMNS = """link_id, recording_id, recording_identity_digest_algorithm,
recording_identity_digest_value, hardware_snapshot_id,
hardware_snapshot_digest_algorithm, hardware_snapshot_digest_value,
link_digest_algorithm, link_digest_value, idempotency_key"""
_VALUES = ", ".join(
    f"%({name.strip()})s" for name in _COLUMNS.replace("\n", " ").split(",")
)
_PUBLISH_SQL = (
    f"INSERT INTO recording_hardware_link ({_COLUMNS}) VALUES ({_VALUES}) "
    "ON CONFLICT DO NOTHING RETURNING link_id"
)
_GET_SQL = f"SELECT {_COLUMNS} FROM recording_hardware_link WHERE recording_id = %(recording_id)s"
_CONFLICT_SQL = (
    f"SELECT {_COLUMNS} FROM recording_hardware_link "
    "WHERE link_id = %(link_id)s OR recording_id = %(recording_id)s "
    "OR link_digest_value = %(link_digest_value)s "
    "OR idempotency_key = %(idempotency_key)s"
)
