"""Capability-limited storage interfaces; concrete adapters live elsewhere."""

from .filesystem import BlobIntegrityError, FileSystemBlobStore
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
    "UnverifiedContinuityError",
    "recover_completed_local_recording",
]
