"""Byte storage and recording-codec ports, with no concrete format choice."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import BinaryIO, Protocol, runtime_checkable

from leo_flow.contracts.capture import (
    CapturePlan,
    CompletedLocalRecording,
    RecordingManifest,
    SegmentManifest,
)
from leo_flow.contracts.continuity import RefillMetadata, SegmentContinuity
from leo_flow.contracts.core import Digest, HardwareSnapshotId, RecordingId, SegmentId
from leo_flow.contracts.storage import (
    ByteRange,
    ObjectMetadata,
    ObjectRef,
    RecordingObjectRef,
)


@runtime_checkable
class BlobWriter(Protocol):
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


@runtime_checkable
class BlobReader(Protocol):
    def head(self, ref: ObjectRef) -> ObjectMetadata: ...

    def open(
        self, ref: ObjectRef, byte_range: ByteRange | None = None
    ) -> AbstractContextManager[BinaryIO]: ...


class RecordingWriteSession(Protocol):
    @property
    def recording_id(self) -> RecordingId: ...

    def append_iq(self, segment_id: SegmentId, ci16_bytes: bytes) -> None: ...

    def finish_segment(self, segment: SegmentManifest) -> None: ...

    def finalize(self, manifest: RecordingManifest) -> CompletedLocalRecording: ...

    def abort(self, reason: str) -> None: ...


class ContinuityRecordingWriteSession(RecordingWriteSession, Protocol):
    def append_refill(
        self, segment_id: SegmentId, ci16_bytes: bytes, metadata: RefillMetadata
    ) -> None: ...

    def record_continuity(
        self, segment_id: SegmentId, continuity: SegmentContinuity
    ) -> None: ...


class RecordingWriter(Protocol):
    def begin(
        self,
        recording_id: RecordingId,
        plan: CapturePlan,
        hardware_metadata_snapshot_id: HardwareSnapshotId,
        destination: str,
    ) -> RecordingWriteSession: ...


class RecordingView(Protocol):
    @property
    def manifest(self) -> RecordingManifest: ...

    def read_iq_bytes(
        self, segment_id: SegmentId, start_sample: int, stop_sample: int
    ) -> bytes: ...

    def continuity(self, segment_id: SegmentId) -> SegmentContinuity | None: ...


class RecordingObjectReader(Protocol):
    def open(
        self, recording_ref: RecordingObjectRef
    ) -> AbstractContextManager[RecordingView]: ...


@dataclass(frozen=True)
class GarbageCollectionCandidate:
    ref: ObjectRef
    unreferenced_since_utc_ns: int


class GarbageCollectionPort(Protocol):
    """Privileged port deliberately absent from ordinary storage capabilities."""

    def collect(
        self, candidates: Iterable[GarbageCollectionCandidate], *, idempotency_key: str
    ) -> tuple[ObjectRef, ...]: ...
