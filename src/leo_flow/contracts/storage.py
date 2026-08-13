"""Storage-facing values that contain no physical format implementation."""

from __future__ import annotations

from dataclasses import dataclass

from ._validation import require_nonnegative, require_positive, require_token
from .core import Digest, RecordingId, canonical_digest


@dataclass(frozen=True)
class ObjectRef:
    digest: Digest
    byte_count: int
    media_type: str
    format_id: str
    locator: str

    def __post_init__(self) -> None:
        require_nonnegative(self.byte_count, "byte_count")
        require_token(self.format_id, "format_id")
        if "/" not in self.media_type:
            raise ValueError("media_type must be a MIME type")
        if not self.locator:
            raise ValueError("opaque locator cannot be empty")


@dataclass(frozen=True)
class ObjectMetadata:
    ref: ObjectRef
    verified: bool


@dataclass(frozen=True)
class ByteRange:
    start: int
    stop: int

    def __post_init__(self) -> None:
        require_nonnegative(self.start, "start")
        require_positive(self.stop, "stop")
        if self.stop <= self.start:
            raise ValueError("byte range is half-open and must be non-empty")


@dataclass(frozen=True)
class RecordingObjectRef:
    """One logical recording backed by an indivisible data/metadata pair."""

    recording_id: RecordingId
    data_object: ObjectRef
    metadata_object: ObjectRef
    manifest_digest: Digest

    def __post_init__(self) -> None:
        if self.data_object.digest == self.metadata_object.digest:
            raise ValueError("recording data and metadata must be distinct objects")
        if self.data_object.byte_count == 0:
            raise ValueError("recording data object cannot be empty")
        if self.metadata_object.byte_count == 0:
            raise ValueError("recording metadata object cannot be empty")

    def identity_digest(self) -> Digest:
        """Hash scientific identity while excluding replaceable locators."""
        return canonical_digest(
            {
                "recording_id": str(self.recording_id),
                "data": {
                    "digest": str(self.data_object.digest),
                    "byte_count": self.data_object.byte_count,
                    "media_type": self.data_object.media_type,
                    "format_id": self.data_object.format_id,
                },
                "metadata": {
                    "digest": str(self.metadata_object.digest),
                    "byte_count": self.metadata_object.byte_count,
                    "media_type": self.metadata_object.media_type,
                    "format_id": self.metadata_object.format_id,
                },
                "manifest_digest": str(self.manifest_digest),
            }
        )


@dataclass(frozen=True)
class PublishedRecordingRef:
    """Catalog-visible recording; both objects became visible atomically."""

    recording_object: RecordingObjectRef

    @property
    def recording_id(self) -> RecordingId:
        return self.recording_object.recording_id
