"""Integrity closure and exact-reference reading for waterfall bundle v0.2."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import Digest, canonical_digest
from leo_flow.contracts.waterfall_v0_2 import (
    WaterfallAnalysisRequestV0_2,
    WaterfallBundleV0_2,
    WaterfallProductRefV0_2,
)
from leo_flow.storage.ports import BlobReader

from .waterfall_v0_2_codec import (
    MAX_WATERFALL_V0_2_BUNDLE_BYTES,
    WATERFALL_V0_2_FORMAT_ID,
    WATERFALL_V0_2_MEDIA_TYPE,
    decode_waterfall_bundle_v0_2,
)


class WaterfallV0_2IntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class WaterfallCatalogProjectionV0_2:
    product_id: str
    analysis_run_id: str
    recording_id: str
    input_recording_digest: Digest
    request_digest: Digest
    tile_count: int
    pixel_count: int


class WaterfallV0_2RefLookup(Protocol):
    def get_waterfall_v0_2(self, product_id: str) -> WaterfallProductRefV0_2 | None: ...


@dataclass(frozen=True)
class DurableWaterfallViewV0_2:
    ref: WaterfallProductRefV0_2
    bundle: WaterfallBundleV0_2


class DurableWaterfallReaderV0_2:
    def __init__(self, blobs: BlobReader, lookup: WaterfallV0_2RefLookup) -> None:
        self._blobs = blobs
        self._lookup = lookup

    def open(self, product_id: str) -> AbstractContextManager[DurableWaterfallViewV0_2]:
        return self._open(product_id)

    @contextmanager
    def _open(self, product_id: str) -> Iterator[DurableWaterfallViewV0_2]:
        ref = self._lookup.get_waterfall_v0_2(product_id)
        if ref is None:
            raise WaterfallV0_2IntegrityError("waterfall v0.2 product was not found")
        blob = ref.bundle_ref
        if (
            blob.media_type != WATERFALL_V0_2_MEDIA_TYPE
            or blob.format_id != WATERFALL_V0_2_FORMAT_ID
            or blob.byte_count > MAX_WATERFALL_V0_2_BUNDLE_BYTES
        ):
            raise WaterfallV0_2IntegrityError("waterfall v0.2 metadata is invalid")
        metadata = self._blobs.head(blob)
        if metadata.ref != blob or not metadata.verified:
            raise WaterfallV0_2IntegrityError("waterfall v0.2 blob is not verified")
        with self._blobs.open(blob) as stream:
            payload = stream.read(MAX_WATERFALL_V0_2_BUNDLE_BYTES + 1)
        if len(payload) != blob.byte_count or Digest.sha256(payload) != blob.digest:
            raise WaterfallV0_2IntegrityError(
                "waterfall v0.2 bytes differ from its reference"
            )
        try:
            bundle = decode_waterfall_bundle_v0_2(payload)
        except ValueError as error:
            raise WaterfallV0_2IntegrityError(
                "waterfall v0.2 bytes are invalid"
            ) from error
        if (
            bundle.product_id != ref.product_id
            or bundle.analysis_run_id != ref.analysis_run_id
            or bundle.recording_id != ref.recording_id
        ):
            raise WaterfallV0_2IntegrityError("waterfall v0.2 identities disagree")
        yield DurableWaterfallViewV0_2(ref, bundle)


def waterfall_projection_v0_2(
    request: WaterfallAnalysisRequestV0_2, bundle: WaterfallBundleV0_2
) -> WaterfallCatalogProjectionV0_2:
    if request.requested_output_schema != bundle.schema:
        raise WaterfallV0_2IntegrityError("request does not select bundle schema")
    recording_digest = request.recording_object_ref.identity_digest()
    if bundle.recording_id != request.recording_id:
        raise WaterfallV0_2IntegrityError("waterfall belongs to another recording")
    if bundle.input_recording_identity_digest != recording_digest:
        raise WaterfallV0_2IntegrityError("waterfall recording identity differs")
    dependencies = tuple(
        sorted(
            request.dependency_refs,
            key=lambda item: (item.artifact_id, str(item.digest)),
        )
    )
    if (
        bundle.provenance.normalized_config_digest != request.config_ref.digest
        or bundle.provenance.input_digests != (recording_digest,)
        or bundle.provenance.dependency_digests
        != (request.algorithm_ref.digest,) + tuple(item.digest for item in dependencies)
    ):
        raise WaterfallV0_2IntegrityError("waterfall provenance differs from request")
    return WaterfallCatalogProjectionV0_2(
        product_id=str(bundle.product_id),
        analysis_run_id=str(bundle.analysis_run_id),
        recording_id=str(bundle.recording_id),
        input_recording_digest=bundle.input_recording_identity_digest,
        request_digest=canonical_digest(request),
        tile_count=len(bundle.tiles),
        pixel_count=sum(
            len(tile.time_bins) * tile.display_frequency_bins for tile in bundle.tiles
        ),
    )
