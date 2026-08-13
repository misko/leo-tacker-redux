"""Storage-facing values that contain no physical format implementation."""

from __future__ import annotations

from dataclasses import dataclass

from ._validation import require_nonnegative, require_positive, require_token
from .core import Digest, RecordingId


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
class PublishedRecordingRef:
    recording_id: RecordingId
    raw_object: ObjectRef
    manifest_digest: Digest
