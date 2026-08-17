"""Authoritative waterfall publication and exact-reference reading."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import Digest, canonical_digest
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.contracts.waterfall import (
    WaterfallAnalysisRequestV0_1,
    WaterfallBundleV0_1,
    WaterfallProductRefV0_1,
)
from leo_flow.storage.ports import BlobReader, BlobWriter

from .waterfall_codec import (
    MAX_WATERFALL_BUNDLE_BYTES,
    WATERFALL_FORMAT_ID,
    WATERFALL_MEDIA_TYPE,
    decode_waterfall_bundle,
    encode_waterfall_bundle,
)


class WaterfallPersistenceError(RuntimeError):
    """Base error for invalid, missing, or contradictory waterfall state."""


class WaterfallNotFoundError(WaterfallPersistenceError):
    pass


class WaterfallIntegrityError(WaterfallPersistenceError):
    pass


@dataclass(frozen=True)
class WaterfallCatalogProjectionV0_1:
    product_id: str
    analysis_run_id: str
    recording_id: str
    input_recording_digest: Digest
    request_digest: Digest
    tile_count: int
    cell_count: int


@dataclass(frozen=True)
class CatalogedWaterfallV0_1:
    projection: WaterfallCatalogProjectionV0_1
    bundle_ref: ObjectRef

    @property
    def ref(self) -> WaterfallProductRefV0_1:
        from leo_flow.contracts.core import AnalysisRunId, RecordingId
        from leo_flow.contracts.waterfall import WaterfallProductId

        return WaterfallProductRefV0_1(
            WaterfallProductId(self.projection.product_id),
            AnalysisRunId(self.projection.analysis_run_id),
            RecordingId(self.projection.recording_id),
            self.bundle_ref,
        )


class WaterfallCatalogV0_1(Protocol):
    def publish_waterfall(
        self,
        projection: WaterfallCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> WaterfallProductRefV0_1: ...

    def get_waterfall(
        self, ref: WaterfallProductRefV0_1
    ) -> CatalogedWaterfallV0_1 | None: ...


class _BlobStore(BlobWriter, BlobReader, Protocol):
    pass


@dataclass(frozen=True)
class DurableWaterfallViewV0_1:
    _ref: WaterfallProductRefV0_1
    _bundle: WaterfallBundleV0_1

    @property
    def ref(self) -> WaterfallProductRefV0_1:
        return self._ref

    def bundle(self) -> WaterfallBundleV0_1:
        return self._bundle


class DurableWaterfallRepositoryV0_1:
    """Publish one bounded blob, then expose one immutable catalog identity."""

    def __init__(self, blobs: _BlobStore, catalog: WaterfallCatalogV0_1) -> None:
        self._blobs = blobs
        self._catalog = catalog

    def publish(
        self,
        request: WaterfallAnalysisRequestV0_1,
        bundle: WaterfallBundleV0_1,
        *,
        idempotency_key: str,
    ) -> WaterfallProductRefV0_1:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        projection = waterfall_projection_v0_1(request, bundle)
        payload = encode_waterfall_bundle(bundle)
        bundle_ref = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=WATERFALL_MEDIA_TYPE,
            format_id=WATERFALL_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:waterfall-bundle-v0.1",
        )
        return self._catalog.publish_waterfall(
            projection,
            bundle_ref,
            request.recording_object_ref,
            idempotency_key=idempotency_key,
        )

    def open(
        self, ref: WaterfallProductRefV0_1
    ) -> AbstractContextManager[DurableWaterfallViewV0_1]:
        return self._open(ref)

    @contextmanager
    def _open(self, ref: WaterfallProductRefV0_1) -> Iterator[DurableWaterfallViewV0_1]:
        cataloged = self._catalog.get_waterfall(ref)
        if cataloged is None:
            raise WaterfallNotFoundError(
                "no waterfall exactly matches the requested reference"
            )
        bundle_ref = cataloged.bundle_ref
        if (
            bundle_ref != ref.bundle_ref
            or bundle_ref.media_type != WATERFALL_MEDIA_TYPE
            or bundle_ref.format_id != WATERFALL_FORMAT_ID
            or bundle_ref.byte_count > MAX_WATERFALL_BUNDLE_BYTES
        ):
            raise WaterfallIntegrityError("waterfall bundle metadata is invalid")
        metadata = self._blobs.head(bundle_ref)
        if metadata.ref != bundle_ref or not metadata.verified:
            raise WaterfallIntegrityError(
                "blob store did not verify exact waterfall metadata"
            )
        with self._blobs.open(bundle_ref) as stream:
            payload = stream.read(MAX_WATERFALL_BUNDLE_BYTES + 1)
        if (
            len(payload) != bundle_ref.byte_count
            or Digest.sha256(payload) != bundle_ref.digest
        ):
            raise WaterfallIntegrityError(
                "waterfall bytes do not match catalog metadata"
            )
        try:
            bundle = decode_waterfall_bundle(payload)
        except ValueError as error:
            raise WaterfallIntegrityError(
                "waterfall bundle bytes are invalid"
            ) from error
        if (
            bundle.product_id != ref.product_id
            or bundle.analysis_run_id != ref.analysis_run_id
            or bundle.recording_id != ref.recording_id
            or _bundle_projection(bundle, cataloged.projection.request_digest)
            != cataloged.projection
        ):
            raise WaterfallIntegrityError(
                "authoritative waterfall disagrees with catalog projection"
            )
        yield DurableWaterfallViewV0_1(ref, bundle)


def waterfall_projection_v0_1(
    request: WaterfallAnalysisRequestV0_1, bundle: WaterfallBundleV0_1
) -> WaterfallCatalogProjectionV0_1:
    """Validate the complete publication closure and derive its projection."""

    if request.requested_output_schema != bundle.schema:
        raise WaterfallIntegrityError("request does not select waterfall schema")
    recording_digest = request.recording_object_ref.identity_digest()
    if bundle.recording_id != request.recording_id:
        raise WaterfallIntegrityError("waterfall belongs to another recording")
    if bundle.input_recording_identity_digest != recording_digest:
        raise WaterfallIntegrityError("waterfall input recording identity differs")
    provenance = bundle.provenance
    if provenance.normalized_config_digest != request.config_ref.digest:
        raise WaterfallIntegrityError("waterfall configuration provenance differs")
    if provenance.input_digests != (recording_digest,):
        raise WaterfallIntegrityError("waterfall input provenance differs")
    dependencies = tuple(
        sorted(
            request.dependency_refs,
            key=lambda item: (item.artifact_id, str(item.digest)),
        )
    )
    expected_dependencies = (request.algorithm_ref.digest,) + tuple(
        item.digest for item in dependencies
    )
    if provenance.dependency_digests != expected_dependencies:
        raise WaterfallIntegrityError("waterfall dependency provenance differs")
    return _bundle_projection(bundle, canonical_digest(request))


def _bundle_projection(
    bundle: WaterfallBundleV0_1, request_digest: Digest
) -> WaterfallCatalogProjectionV0_1:
    return WaterfallCatalogProjectionV0_1(
        product_id=str(bundle.product_id),
        analysis_run_id=str(bundle.analysis_run_id),
        recording_id=str(bundle.recording_id),
        input_recording_digest=bundle.input_recording_identity_digest,
        request_digest=request_digest,
        tile_count=len(bundle.tiles),
        cell_count=sum(
            len(tile.time_bins) * len(tile.frequency_bin_offsets_hz)
            for tile in bundle.tiles
        ),
    )
