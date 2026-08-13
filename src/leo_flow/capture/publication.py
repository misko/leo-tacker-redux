"""Idempotent, restart-safe publication of locally completed recordings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.capture import CompletedLocalRecording
from leo_flow.contracts.ports import RecordingPublisher
from leo_flow.contracts.storage import PublishedRecordingRef

from .spool import SQLiteLocalSpool


class LocalRecordingCleaner(Protocol):
    """Capture-local cleanup; opaque object locators remain adapter-owned."""

    def cleanup(self, recording: CompletedLocalRecording) -> None: ...


@dataclass(frozen=True)
class ReconciliationResult:
    published: int = 0
    cleaned: int = 0
    deferred: int = 0
    errors: tuple[str, ...] = ()


class PublicationReconciler:
    def __init__(
        self,
        spool: SQLiteLocalSpool,
        publisher: RecordingPublisher,
        cleaner: LocalRecordingCleaner,
    ) -> None:
        self._spool = spool
        self._publisher = publisher
        self._cleaner = cleaner

    def reconcile(self, limit: int = 100) -> ReconciliationResult:
        if limit <= 0:
            raise ValueError("limit must be positive")
        published_count = 0
        cleaned_count = 0
        deferred_count = 0
        errors: list[str] = []

        acknowledged = self._spool.pending_cleanup(limit)
        for recording in acknowledged:
            try:
                self._cleaner.cleanup(recording)
                self._spool.mark_cleaned(recording.recording_id)
                cleaned_count += 1
            except Exception as error:  # noqa: BLE001 - adapter boundary defers any failure
                deferred_count += 1
                errors.append(_error(recording, "cleanup", error))

        remaining = max(0, limit - len(acknowledged))
        for recording in (
            self._spool.pending_publication(remaining) if remaining else ()
        ):
            key = publication_idempotency_key(recording)
            try:
                published = self._publisher.publish(recording, idempotency_key=key)
                _validate_publication(recording, published)
            except Exception as error:  # noqa: BLE001 - publisher may be remote/plugin code
                self._spool.note_publish_attempt(
                    recording.recording_id, key, f"{type(error).__name__}: {error}"
                )
                deferred_count += 1
                errors.append(_error(recording, "publication", error))
                continue
            try:
                self._spool.note_publish_attempt(recording.recording_id, key, None)
                self._spool.acknowledge(recording, published, key)
            except Exception as error:  # noqa: BLE001 - local durability boundary
                # The remote operation may already have committed. Leave the
                # row complete and retry with the same key after SQLite heals.
                deferred_count += 1
                errors.append(_error(recording, "acknowledgement", error))
                continue
            published_count += 1
            try:
                self._cleaner.cleanup(recording)
                self._spool.mark_cleaned(recording.recording_id)
                cleaned_count += 1
            except Exception as error:  # noqa: BLE001 - adapter boundary defers any failure
                deferred_count += 1
                errors.append(_error(recording, "cleanup", error))

        return ReconciliationResult(
            published_count, cleaned_count, deferred_count, tuple(errors)
        )


def publication_idempotency_key(recording: CompletedLocalRecording) -> str:
    return f"recording:{recording.recording_id}:{recording.manifest_digest}"


def _validate_publication(
    recording: CompletedLocalRecording, published: PublishedRecordingRef
) -> None:
    remote = published.recording_object
    expected = (
        recording.recording_id,
        recording.data_object.digest,
        recording.data_object.byte_count,
        recording.metadata_object.digest,
        recording.metadata_object.byte_count,
        recording.manifest_digest,
    )
    actual = (
        remote.recording_id,
        remote.data_object.digest,
        remote.data_object.byte_count,
        remote.metadata_object.digest,
        remote.metadata_object.byte_count,
        remote.manifest_digest,
    )
    if actual != expected:
        raise ValueError("publisher returned a different recording identity")


def _error(recording: CompletedLocalRecording, operation: str, error: Exception) -> str:
    return f"{recording.recording_id}:{operation}:{type(error).__name__}:{error}"
