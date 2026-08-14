"""Psycopg-backed atomic recording-pair catalog and publication adapter."""

from __future__ import annotations

from collections.abc import Callable
from typing import BinaryIO, Protocol

import psycopg
from psycopg.rows import dict_row

from leo_flow.contracts.capture import CompletedLocalRecording
from leo_flow.contracts.core import Digest, DigestAlgorithm, RecordingId
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)

from . import postgres_sql
from .ports import LocalRecordingSource


class PostgresCatalogError(RuntimeError):
    pass


class ObjectCollisionError(PostgresCatalogError):
    pass


class RecordingConflictError(PostgresCatalogError):
    pass


ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class _BlobWriter(Protocol):
    def put(
        self,
        stream: BinaryIO,
        *,
        expected_digest: Digest,
        expected_bytes: int,
        media_type: str,
        format_id: str,
        idempotency_key: str,
    ) -> ObjectRef: ...


class PostgresRecordingCatalog:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish(
        self, recording: RecordingObjectRef, *, idempotency_key: str
    ) -> PublishedRecordingRef:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            self._register_object(cursor, recording.data_object)
            self._register_object(cursor, recording.metadata_object)
            cursor.execute(
                postgres_sql.PUBLISH_RECORDING_SQL,
                _recording_parameters(recording, idempotency_key),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                cursor.execute(
                    """
                    SELECT recording_id, idempotency_key
                    FROM recording
                    WHERE recording_id = %(recording_id)s
                       OR idempotency_key = %(idempotency_key)s
                    """,
                    {
                        "recording_id": str(recording.recording_id),
                        "idempotency_key": idempotency_key,
                    },
                )
                conflicts = cursor.fetchall()
                if len(conflicts) != 1:
                    raise RecordingConflictError(
                        "recording ID and idempotency key identify different rows"
                    )
                if (
                    conflicts[0]["recording_id"] != str(recording.recording_id)
                    or conflicts[0]["idempotency_key"] != idempotency_key
                ):
                    raise RecordingConflictError(
                        "recording ID or idempotency key was reused"
                    )
                existing = self._get_with_cursor(
                    cursor, str(conflicts[0]["recording_id"])
                )
                if existing is None or existing.recording_object != recording:
                    raise RecordingConflictError(
                        "recording or idempotency key identifies different content"
                    )
                return existing
            return PublishedRecordingRef(recording)

    def get(self, recording_id: RecordingId) -> PublishedRecordingRef | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            return self._get_with_cursor(cursor, str(recording_id))

    @staticmethod
    def _register_object(
        cursor: psycopg.Cursor[dict[str, object]], ref: ObjectRef
    ) -> None:
        parameters = _object_parameters(ref)
        cursor.execute(postgres_sql.REGISTER_OBJECT_SQL, parameters)
        cursor.execute(postgres_sql.VERIFY_OBJECT_SQL, parameters)
        row = cursor.fetchone()
        if row is None or (
            _database_int(row["byte_count"], "byte_count") != ref.byte_count
            or row["media_type"] != ref.media_type
            or row["format_id"] != ref.format_id
            or row["locator"] != ref.locator
        ):
            raise ObjectCollisionError(
                f"object digest {ref.digest} identifies different metadata"
            )

    @staticmethod
    def _get_with_cursor(
        cursor: psycopg.Cursor[dict[str, object]], recording_id: str
    ) -> PublishedRecordingRef | None:
        cursor.execute(postgres_sql.GET_RECORDING_SQL, {"recording_id": recording_id})
        row = cursor.fetchone()
        return None if row is None else _published_from_row(row)


class PostgresRecordingPublisher:
    """Upload both objects first; expose only their atomic catalog pair."""

    def __init__(
        self,
        local: LocalRecordingSource,
        blobs: _BlobWriter,
        catalog: PostgresRecordingCatalog,
    ) -> None:
        self._local = local
        self._blobs = blobs
        self._catalog = catalog

    def publish(
        self, recording: CompletedLocalRecording, *, idempotency_key: str
    ) -> PublishedRecordingRef:
        with self._local.open_data(recording) as stream:
            data = self._upload(
                stream,
                recording.data_object.digest,
                recording.data_object.byte_count,
                "application/octet-stream",
                "leo-recording-data-v1",
                f"{idempotency_key}:data",
            )
        with self._local.open_metadata(recording) as stream:
            metadata = self._upload(
                stream,
                recording.metadata_object.digest,
                recording.metadata_object.byte_count,
                "application/json",
                "leo-recording-metadata-v1",
                f"{idempotency_key}:metadata",
            )
        pair = RecordingObjectRef(
            recording.recording_id, data, metadata, recording.manifest_digest
        )
        return self._catalog.publish(pair, idempotency_key=idempotency_key)

    def _upload(
        self,
        stream: BinaryIO,
        digest: Digest,
        byte_count: int,
        media_type: str,
        format_id: str,
        idempotency_key: str,
    ) -> ObjectRef:
        return self._blobs.put(
            stream,
            expected_digest=digest,
            expected_bytes=byte_count,
            media_type=media_type,
            format_id=format_id,
            idempotency_key=idempotency_key,
        )


def connection_factory(dsn: str) -> ConnectionFactory:
    return lambda: psycopg.connect(dsn, row_factory=dict_row)


def _object_parameters(ref: ObjectRef) -> dict[str, object]:
    return {
        "digest_algorithm": ref.digest.algorithm.value,
        "digest_value": ref.digest.value,
        "byte_count": ref.byte_count,
        "media_type": ref.media_type,
        "format_id": ref.format_id,
        "locator": ref.locator,
    }


def _recording_parameters(
    ref: RecordingObjectRef, idempotency_key: str
) -> dict[str, object]:
    return {
        "recording_id": str(ref.recording_id),
        "data_digest_algorithm": ref.data_object.digest.algorithm.value,
        "data_digest_value": ref.data_object.digest.value,
        "metadata_digest_algorithm": ref.metadata_object.digest.algorithm.value,
        "metadata_digest_value": ref.metadata_object.digest.value,
        "manifest_digest_value": ref.manifest_digest.value,
        "idempotency_key": idempotency_key,
    }


def _published_from_row(row: dict[str, object]) -> PublishedRecordingRef:
    data = ObjectRef(
        Digest(
            DigestAlgorithm(str(row["data_digest_algorithm"])),
            str(row["data_digest_value"]),
        ),
        _database_int(row["data_byte_count"], "data_byte_count"),
        str(row["data_media_type"]),
        str(row["data_format_id"]),
        str(row["data_locator"]),
    )
    metadata = ObjectRef(
        Digest(
            DigestAlgorithm(str(row["metadata_digest_algorithm"])),
            str(row["metadata_digest_value"]),
        ),
        _database_int(row["metadata_byte_count"], "metadata_byte_count"),
        str(row["metadata_media_type"]),
        str(row["metadata_format_id"]),
        str(row["metadata_locator"]),
    )
    pair = RecordingObjectRef(
        RecordingId(str(row["recording_id"])),
        data,
        metadata,
        Digest(DigestAlgorithm.SHA256, str(row["manifest_digest_value"])),
    )
    return PublishedRecordingRef(pair)


def _database_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PostgresCatalogError(f"database {field} is not an integer")
    return value
