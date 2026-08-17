"""Integrity closure for durable v0.2 Starlink detector-suite products."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import Digest, canonical_digest
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteProductRefV0_2,
    StarlinkDetectorSuiteRecordingBundleV0_2,
    StarlinkDetectorSuiteRequestV0_2,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .starlink_suite_codec import (
    MAX_STARLINK_SUITE_BUNDLE_BYTES,
    STARLINK_SUITE_FORMAT_ID,
    STARLINK_SUITE_MEDIA_TYPE,
    decode_starlink_suite_bundle,
    encode_starlink_suite_bundle,
)


class StarlinkSuiteIntegrityError(ValueError):
    pass


class StarlinkSuiteNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class StarlinkSuiteCatalogProjectionV0_2:
    analysis_id: str
    recording_id: str
    input_recording_digest: Digest
    request_digest: Digest
    state: str
    suite_count: int
    method_count: int


@dataclass(frozen=True)
class CatalogedStarlinkSuiteV0_2:
    projection: StarlinkSuiteCatalogProjectionV0_2
    bundle_ref: ObjectRef

    @property
    def ref(self) -> StarlinkDetectorSuiteProductRefV0_2:
        from leo_flow.contracts.core import RecordingId

        return StarlinkDetectorSuiteProductRefV0_2(
            self.projection.analysis_id,
            RecordingId(self.projection.recording_id),
            self.bundle_ref,
        )


class StarlinkSuiteCatalogV0_2(Protocol):
    def publish_starlink_suite(
        self,
        projection: StarlinkSuiteCatalogProjectionV0_2,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkDetectorSuiteProductRefV0_2: ...
    def get_starlink_suite(
        self, ref: StarlinkDetectorSuiteProductRefV0_2
    ) -> CatalogedStarlinkSuiteV0_2 | None: ...


class StarlinkSuiteBlobStore(BlobReader, BlobWriter, Protocol):
    pass


class DurableStarlinkSuiteStoreV0_2:
    def __init__(
        self, blobs: StarlinkSuiteBlobStore, catalog: StarlinkSuiteCatalogV0_2
    ) -> None:
        self._blobs, self._catalog = blobs, catalog

    def publish(
        self,
        request: StarlinkDetectorSuiteRequestV0_2,
        bundle: StarlinkDetectorSuiteRecordingBundleV0_2,
        *,
        idempotency_key: str,
    ) -> StarlinkDetectorSuiteProductRefV0_2:
        projection = starlink_suite_projection_v0_2(request, bundle)
        payload = encode_starlink_suite_bundle(bundle)
        ref = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=STARLINK_SUITE_MEDIA_TYPE,
            format_id=STARLINK_SUITE_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:bundle-v0.2",
        )
        return self._catalog.publish_starlink_suite(
            projection,
            ref,
            request.recording_object_ref,
            idempotency_key=idempotency_key,
        )

    @contextmanager
    def open(
        self, ref: StarlinkDetectorSuiteProductRefV0_2
    ) -> Iterator[StarlinkDetectorSuiteRecordingBundleV0_2]:
        cataloged = self._catalog.get_starlink_suite(ref)
        if cataloged is None:
            raise StarlinkSuiteNotFoundError("detector-suite result was not found")
        bundle_ref = cataloged.bundle_ref
        if (
            bundle_ref != ref.bundle_ref
            or bundle_ref.media_type != STARLINK_SUITE_MEDIA_TYPE
            or bundle_ref.format_id != STARLINK_SUITE_FORMAT_ID
            or bundle_ref.byte_count > MAX_STARLINK_SUITE_BUNDLE_BYTES
        ):
            raise StarlinkSuiteIntegrityError(
                "detector-suite bundle metadata is invalid"
            )
        metadata = self._blobs.head(bundle_ref)
        if metadata.ref != bundle_ref or not metadata.verified:
            raise StarlinkSuiteIntegrityError("detector-suite blob is not verified")
        with self._blobs.open(bundle_ref) as stream:
            payload = stream.read(MAX_STARLINK_SUITE_BUNDLE_BYTES + 1)
        if (
            len(payload) != bundle_ref.byte_count
            or Digest.sha256(payload) != bundle_ref.digest
        ):
            raise StarlinkSuiteIntegrityError("detector-suite bytes differ")
        try:
            bundle = decode_starlink_suite_bundle(payload)
        except ValueError as error:
            raise StarlinkSuiteIntegrityError(
                "detector-suite bundle is invalid"
            ) from error
        if (
            bundle.analysis_id != ref.analysis_id
            or bundle.recording_id != ref.recording_id
            or _projection(bundle, cataloged.projection.request_digest)
            != cataloged.projection
        ):
            raise StarlinkSuiteIntegrityError(
                "detector-suite catalog and bundle differ"
            )
        yield bundle


def starlink_suite_projection_v0_2(
    request: StarlinkDetectorSuiteRequestV0_2,
    bundle: StarlinkDetectorSuiteRecordingBundleV0_2,
) -> StarlinkSuiteCatalogProjectionV0_2:
    if (
        request.requested_output_schema != bundle.schema
        or request.recording_id != bundle.recording_id
        or request.recording_object_ref.identity_digest()
        != bundle.recording_identity_digest
    ):
        raise StarlinkSuiteIntegrityError("detector-suite request and bundle differ")
    expected = {
        (item.segment_id, item.receiver_chain_id) for item in request.stream_selections
    }
    actual = {(item.segment_id, item.receiver_chain_id) for item in bundle.suites}
    if expected != actual:
        raise StarlinkSuiteIntegrityError("detector-suite stream membership differs")
    return _projection(bundle, canonical_digest(request))


def _projection(
    bundle: StarlinkDetectorSuiteRecordingBundleV0_2, request_digest: Digest
) -> StarlinkSuiteCatalogProjectionV0_2:
    return StarlinkSuiteCatalogProjectionV0_2(
        bundle.analysis_id,
        str(bundle.recording_id),
        bundle.recording_identity_digest,
        request_digest,
        bundle.state.value,
        len(bundle.suites),
        sum(len(item.methods) for item in bundle.suites),
    )
