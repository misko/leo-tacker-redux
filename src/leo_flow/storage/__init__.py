"""Capability-limited storage interfaces; concrete adapters live elsewhere."""

from .filesystem import BlobIntegrityError, FileSystemBlobStore
from .legacy_recording import (
    LegacyFileRef,
    LegacyRecordingError,
    LegacyRecordingReader,
    LegacyRecordingRegistration,
    UnsupportedLegacyRecordingError,
    legacy_selected_chunk_index_digest,
)
from .local_recording import (
    LocalRecordingNotFinalizedError,
    LocalRecordingSecurityError,
    RootedSigMFRecordingStore,
)
from .ports import (
    BlobReader,
    BlobWriter,
    GarbageCollectionPort,
    LocalRecordingSource,
    RecordingObjectReader,
    RecordingView,
    RecordingWriter,
    RecordingWriteSession,
)
from .recording_codec import (
    SigMFRecordingObjectReader,
    SigMFRecordingWriter,
    UnverifiedContinuityError,
    recover_completed_local_recording,
)

__all__ = [
    "BlobIntegrityError",
    "BlobReader",
    "BlobWriter",
    "FileSystemBlobStore",
    "GarbageCollectionPort",
    "LegacyFileRef",
    "LegacyRecordingError",
    "LegacyRecordingReader",
    "LegacyRecordingRegistration",
    "LocalRecordingNotFinalizedError",
    "LocalRecordingSecurityError",
    "LocalRecordingSource",
    "RecordingObjectReader",
    "RecordingView",
    "RecordingWriteSession",
    "RecordingWriter",
    "RootedSigMFRecordingStore",
    "SigMFRecordingObjectReader",
    "SigMFRecordingWriter",
    "UnsupportedLegacyRecordingError",
    "UnverifiedContinuityError",
    "legacy_selected_chunk_index_digest",
    "recover_completed_local_recording",
]
