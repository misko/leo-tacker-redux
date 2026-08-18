"""CAS-first immutable persistence for the independent IQ timeline."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import Digest, RecordingId
from leo_flow.contracts.starlink_full_dwell_timeline_product import (
    FullDwellTimelineBundleV0_1,
    FullDwellTimelineProductRefV0_1,
    FullDwellTimelineRequestV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .starlink_full_dwell_timeline_codec import (
    FULL_DWELL_TIMELINE_FORMAT_ID,
    FULL_DWELL_TIMELINE_MEDIA_TYPE,
    MAX_FULL_DWELL_TIMELINE_BYTES,
    decode_full_dwell_timeline,
    encode_full_dwell_timeline,
)


class FullDwellTimelineIntegrityError(RuntimeError):
    pass


class FullDwellTimelineConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class FullDwellTimelineCatalogProjectionV0_1:
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    request_digest: Digest
    stream_count: int
    window_count: int
    covered_sample_count: int


@dataclass(frozen=True)
class CatalogedFullDwellTimelineV0_1:
    projection: FullDwellTimelineCatalogProjectionV0_1
    bundle_ref: ObjectRef

    @property
    def ref(self) -> FullDwellTimelineProductRefV0_1:
        return FullDwellTimelineProductRefV0_1(
            self.projection.analysis_id, self.projection.recording_id, self.bundle_ref
        )


class FullDwellTimelineCatalogV0_1(Protocol):
    def publish_full_dwell_timeline(
        self,
        projection: FullDwellTimelineCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> FullDwellTimelineProductRefV0_1: ...

    def get_full_dwell_timeline(
        self, ref: FullDwellTimelineProductRefV0_1
    ) -> CatalogedFullDwellTimelineV0_1 | None: ...


class FullDwellTimelineBlobStore(BlobReader, BlobWriter, Protocol):
    pass


class DurableFullDwellTimelineStoreV0_1:
    def __init__(
        self, blobs: FullDwellTimelineBlobStore, catalog: FullDwellTimelineCatalogV0_1
    ) -> None:
        self._blobs, self._catalog = blobs, catalog

    def publish(
        self,
        request: FullDwellTimelineRequestV0_1,
        bundle: FullDwellTimelineBundleV0_1,
        *,
        idempotency_key: str,
    ) -> FullDwellTimelineProductRefV0_1:
        if not idempotency_key:
            raise ValueError("idempotency key cannot be empty")
        projection = full_dwell_timeline_projection_v0_1(request, bundle)
        payload = encode_full_dwell_timeline(bundle)
        blob = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=FULL_DWELL_TIMELINE_MEDIA_TYPE,
            format_id=FULL_DWELL_TIMELINE_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:bundle-v0.1",
        )
        expected = FullDwellTimelineProductRefV0_1(
            bundle.analysis_id, bundle.recording_id, blob
        )
        actual = self._catalog.publish_full_dwell_timeline(
            projection,
            blob,
            request.recording_object_ref,
            idempotency_key=idempotency_key,
        )
        if actual != expected:
            raise FullDwellTimelineConflictError(
                "catalog replay returned another timeline product"
            )
        return actual

    def open(
        self, ref: FullDwellTimelineProductRefV0_1
    ) -> AbstractContextManager[FullDwellTimelineBundleV0_1]:
        return self._open(ref)

    @contextmanager
    def _open(
        self, ref: FullDwellTimelineProductRefV0_1
    ) -> Iterator[FullDwellTimelineBundleV0_1]:
        cataloged = self._catalog.get_full_dwell_timeline(ref)
        if cataloged is None or cataloged.ref != ref:
            raise LookupError("timeline product was not found")
        blob = cataloged.bundle_ref
        if (
            blob.media_type != FULL_DWELL_TIMELINE_MEDIA_TYPE
            or blob.format_id != FULL_DWELL_TIMELINE_FORMAT_ID
            or blob.byte_count > MAX_FULL_DWELL_TIMELINE_BYTES
        ):
            raise FullDwellTimelineIntegrityError("timeline metadata is invalid")
        metadata = self._blobs.head(blob)
        if metadata.ref != blob or not metadata.verified:
            raise FullDwellTimelineIntegrityError("timeline blob is not verified")
        with self._blobs.open(blob) as stream:
            payload = stream.read(MAX_FULL_DWELL_TIMELINE_BYTES + 1)
        if len(payload) != blob.byte_count or Digest.sha256(payload) != blob.digest:
            raise FullDwellTimelineIntegrityError("timeline bytes differ")
        try:
            bundle = decode_full_dwell_timeline(payload)
        except ValueError as error:
            raise FullDwellTimelineIntegrityError(
                "timeline bundle is invalid"
            ) from error
        if _projection(bundle) != cataloged.projection:
            raise FullDwellTimelineIntegrityError("timeline catalog and bundle differ")
        yield bundle


def full_dwell_timeline_projection_v0_1(
    request: FullDwellTimelineRequestV0_1,
    bundle: FullDwellTimelineBundleV0_1,
) -> FullDwellTimelineCatalogProjectionV0_1:
    if (
        request.recording_id != bundle.recording_id
        or request.recording_object_ref.identity_digest()
        != bundle.recording_identity_digest
        or request.digest != bundle.request_digest
        or request.plan != bundle.plan
        or request.requested_output_schema != bundle.schema
    ):
        raise FullDwellTimelineIntegrityError("timeline request and bundle differ")
    expected = {item.identity for item in request.stream_selections}
    actual = {item.identity for item in bundle.streams}
    if expected != actual:
        raise FullDwellTimelineIntegrityError("timeline stream identities differ")
    return _projection(bundle)


def _projection(
    bundle: FullDwellTimelineBundleV0_1,
) -> FullDwellTimelineCatalogProjectionV0_1:
    return FullDwellTimelineCatalogProjectionV0_1(
        bundle.analysis_id,
        bundle.recording_id,
        bundle.recording_identity_digest,
        bundle.request_digest,
        len(bundle.streams),
        sum(len(stream.windows) for stream in bundle.streams),
        sum(stream.covered_sample_count for stream in bundle.streams),
    )
