"""First-slice atomic recording catalog and publisher conformance adapter."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import BinaryIO, Protocol

from leo_flow.contracts.capture import CompletedLocalRecording
from leo_flow.contracts.core import Digest
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)

from .filesystem import IdempotencyConflictError


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


class RecordingConflictError(RuntimeError):
    pass


class InMemoryRecordingCatalog:
    """Atomic reference fake; it makes no PostgreSQL concurrency claim."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._recordings: dict[str, PublishedRecordingRef] = {}
        self._idempotency: dict[str, RecordingObjectRef] = {}

    def publish(
        self, recording: RecordingObjectRef, *, idempotency_key: str
    ) -> PublishedRecordingRef:
        with self._lock:
            by_key = self._idempotency.get(idempotency_key)
            if by_key is not None:
                if by_key != recording:
                    raise IdempotencyConflictError(
                        "recording idempotency key identifies a different pair"
                    )
                return PublishedRecordingRef(by_key)
            existing = self._recordings.get(str(recording.recording_id))
            if existing is not None:
                if existing.recording_object != recording:
                    raise RecordingConflictError(
                        "recording ID already identifies a different pair"
                    )
                self._idempotency[idempotency_key] = recording
                return existing
            published = PublishedRecordingRef(recording)
            self._recordings[str(recording.recording_id)] = published
            self._idempotency[idempotency_key] = recording
            return published

    def get(self, recording_id: str) -> PublishedRecordingRef | None:
        with self._lock:
            return self._recordings.get(recording_id)


class RecordingPublisherAdapter:
    """Upload both local objects, then expose their pair in one catalog call."""

    def __init__(self, blobs: _BlobWriter, catalog: InMemoryRecordingCatalog) -> None:
        self._blobs = blobs
        self._catalog = catalog

    def publish(
        self, recording: CompletedLocalRecording, *, idempotency_key: str
    ) -> PublishedRecordingRef:
        data = self._upload(
            recording.data_object.locator,
            recording.data_object.digest,
            recording.data_object.byte_count,
            "application/octet-stream",
            "leo-recording-data-v1",
            f"{idempotency_key}:data",
        )
        metadata = self._upload(
            recording.metadata_object.locator,
            recording.metadata_object.digest,
            recording.metadata_object.byte_count,
            "application/json",
            "leo-recording-metadata-v1",
            f"{idempotency_key}:metadata",
        )
        logical = RecordingObjectRef(
            recording.recording_id, data, metadata, recording.manifest_digest
        )
        return self._catalog.publish(logical, idempotency_key=idempotency_key)

    def _upload(
        self,
        locator: str,
        digest: Digest,
        byte_count: int,
        media_type: str,
        format_id: str,
        idempotency_key: str,
    ) -> ObjectRef:
        with Path(locator).open("rb") as stream:
            return self._blobs.put(
                stream,
                expected_digest=digest,
                expected_bytes=byte_count,
                media_type=media_type,
                format_id=format_id,
                idempotency_key=idempotency_key,
            )
