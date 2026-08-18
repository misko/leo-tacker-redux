"""CAS-first persistence for complete-IQ pilot-prescreen evidence."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import Digest, RecordingId
from leo_flow.contracts.starlink_pilot_prescreen import (
    StarlinkPilotPrescreenBundleV0_1,
    StarlinkPilotPrescreenProductRefV0_1,
    StarlinkPilotPrescreenRequestV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .starlink_pilot_prescreen_codec import (
    MAXIMUM_PILOT_PRESCREEN_BYTES,
    PILOT_PRESCREEN_FORMAT_ID,
    PILOT_PRESCREEN_MEDIA_TYPE,
    decode_starlink_pilot_prescreen,
    encode_starlink_pilot_prescreen,
)


class StarlinkPilotPrescreenIntegrityError(RuntimeError):
    pass


class StarlinkPilotPrescreenConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StarlinkPilotPrescreenCatalogProjectionV0_1:
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    request_digest: Digest
    stream_count: int
    window_count: int
    analyzed_sample_count: int
    selected_window_count: int


@dataclass(frozen=True, slots=True)
class CatalogedStarlinkPilotPrescreenV0_1:
    projection: StarlinkPilotPrescreenCatalogProjectionV0_1
    bundle_ref: ObjectRef

    @property
    def ref(self) -> StarlinkPilotPrescreenProductRefV0_1:
        return StarlinkPilotPrescreenProductRefV0_1(
            self.projection.analysis_id,
            self.projection.recording_id,
            self.bundle_ref,
        )


class StarlinkPilotPrescreenCatalogV0_1(Protocol):
    def publish_starlink_pilot_prescreen(
        self,
        projection: StarlinkPilotPrescreenCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkPilotPrescreenProductRefV0_1: ...

    def get_starlink_pilot_prescreen(
        self, ref: StarlinkPilotPrescreenProductRefV0_1
    ) -> CatalogedStarlinkPilotPrescreenV0_1 | None: ...


class StarlinkPilotPrescreenBlobStore(BlobReader, BlobWriter, Protocol):
    pass


class DurableStarlinkPilotPrescreenStoreV0_1:
    def __init__(
        self,
        blobs: StarlinkPilotPrescreenBlobStore,
        catalog: StarlinkPilotPrescreenCatalogV0_1,
    ) -> None:
        self._blobs, self._catalog = blobs, catalog

    def publish(
        self,
        request: StarlinkPilotPrescreenRequestV0_1,
        bundle: StarlinkPilotPrescreenBundleV0_1,
        *,
        idempotency_key: str,
    ) -> StarlinkPilotPrescreenProductRefV0_1:
        if not idempotency_key:
            raise ValueError("pilot-prescreen idempotency key cannot be empty")
        projection = starlink_pilot_prescreen_projection_v0_1(request, bundle)
        payload = encode_starlink_pilot_prescreen(bundle)
        blob = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=PILOT_PRESCREEN_MEDIA_TYPE,
            format_id=PILOT_PRESCREEN_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:bundle-v0.1",
        )
        expected = StarlinkPilotPrescreenProductRefV0_1(
            bundle.analysis_id, bundle.recording_id, blob
        )
        actual = self._catalog.publish_starlink_pilot_prescreen(
            projection,
            blob,
            request.recording_object_ref,
            idempotency_key=idempotency_key,
        )
        if actual != expected:
            raise StarlinkPilotPrescreenConflictError(
                "pilot-prescreen catalog returned another product"
            )
        return actual

    def open(
        self, ref: StarlinkPilotPrescreenProductRefV0_1
    ) -> AbstractContextManager[StarlinkPilotPrescreenBundleV0_1]:
        return self._open(ref)

    @contextmanager
    def _open(
        self, ref: StarlinkPilotPrescreenProductRefV0_1
    ) -> Iterator[StarlinkPilotPrescreenBundleV0_1]:
        cataloged = self._catalog.get_starlink_pilot_prescreen(ref)
        if cataloged is None or cataloged.ref != ref:
            raise LookupError("pilot-prescreen product was not found")
        blob = cataloged.bundle_ref
        if (
            blob.media_type != PILOT_PRESCREEN_MEDIA_TYPE
            or blob.format_id != PILOT_PRESCREEN_FORMAT_ID
            or blob.byte_count > MAXIMUM_PILOT_PRESCREEN_BYTES
        ):
            raise StarlinkPilotPrescreenIntegrityError(
                "pilot-prescreen blob metadata is invalid"
            )
        metadata = self._blobs.head(blob)
        if metadata.ref != blob or not metadata.verified:
            raise StarlinkPilotPrescreenIntegrityError(
                "pilot-prescreen blob is not verified"
            )
        with self._blobs.open(blob) as stream:
            payload = stream.read(MAXIMUM_PILOT_PRESCREEN_BYTES + 1)
        if len(payload) != blob.byte_count or Digest.sha256(payload) != blob.digest:
            raise StarlinkPilotPrescreenIntegrityError(
                "pilot-prescreen blob bytes differ"
            )
        try:
            bundle = decode_starlink_pilot_prescreen(payload)
        except ValueError as error:
            raise StarlinkPilotPrescreenIntegrityError(
                "pilot-prescreen bundle is invalid"
            ) from error
        if _projection(bundle) != cataloged.projection:
            raise StarlinkPilotPrescreenIntegrityError(
                "pilot-prescreen catalog and bundle differ"
            )
        yield bundle


def starlink_pilot_prescreen_projection_v0_1(
    request: StarlinkPilotPrescreenRequestV0_1,
    bundle: StarlinkPilotPrescreenBundleV0_1,
) -> StarlinkPilotPrescreenCatalogProjectionV0_1:
    if (
        request.recording_id != bundle.recording_id
        or request.recording_object_ref.identity_digest()
        != bundle.recording_identity_digest
        or request.digest != bundle.request_digest
        or request.plan != bundle.plan
    ):
        raise StarlinkPilotPrescreenIntegrityError(
            "pilot-prescreen request and bundle differ"
        )
    expected = {item.identity for item in request.streams}
    actual = {item.selection.identity for item in bundle.streams}
    if expected != actual:
        raise StarlinkPilotPrescreenIntegrityError(
            "pilot-prescreen stream identities differ"
        )
    return _projection(bundle)


def _projection(
    bundle: StarlinkPilotPrescreenBundleV0_1,
) -> StarlinkPilotPrescreenCatalogProjectionV0_1:
    return StarlinkPilotPrescreenCatalogProjectionV0_1(
        bundle.analysis_id,
        bundle.recording_id,
        bundle.recording_identity_digest,
        bundle.request_digest,
        len(bundle.streams),
        sum(len(stream.windows) for stream in bundle.streams),
        sum(stream.analyzed_sample_count for stream in bundle.streams),
        sum(
            item.selected_for_exact_refinement
            for stream in bundle.streams
            for item in stream.windows
        ),
    )
