"""Integrity closure for durable Starlink candidate bundles."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import Digest, canonical_digest
from leo_flow.contracts.starlink import StarlinkPilotAnalysisBundleV0_1
from leo_flow.contracts.starlink_pipeline import (
    StarlinkPilotAnalysisProductRefV0_1,
    StarlinkPilotAnalysisRequestV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .starlink_codec import (
    MAX_STARLINK_BUNDLE_BYTES,
    STARLINK_FORMAT_ID,
    STARLINK_MEDIA_TYPE,
    decode_starlink_bundle,
    encode_starlink_bundle,
)


class StarlinkIntegrityError(ValueError):
    pass


class StarlinkNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class StarlinkCatalogProjectionV0_1:
    analysis_id: str
    recording_id: str
    input_recording_digest: Digest
    request_digest: Digest
    candidate_count: int
    analyzed_stream_count: int


@dataclass(frozen=True)
class CatalogedStarlinkV0_1:
    projection: StarlinkCatalogProjectionV0_1
    bundle_ref: ObjectRef

    @property
    def ref(self) -> StarlinkPilotAnalysisProductRefV0_1:
        from leo_flow.contracts.core import RecordingId

        return StarlinkPilotAnalysisProductRefV0_1(
            self.projection.analysis_id,
            RecordingId(self.projection.recording_id),
            self.bundle_ref,
        )


class StarlinkCatalogV0_1(Protocol):
    def publish_starlink(
        self,
        projection: StarlinkCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkPilotAnalysisProductRefV0_1: ...

    def get_starlink(
        self, ref: StarlinkPilotAnalysisProductRefV0_1
    ) -> CatalogedStarlinkV0_1 | None: ...


@dataclass(frozen=True)
class DurableStarlinkViewV0_1:
    ref: StarlinkPilotAnalysisProductRefV0_1
    _bundle: StarlinkPilotAnalysisBundleV0_1

    def bundle(self) -> StarlinkPilotAnalysisBundleV0_1:
        return self._bundle


class DurableStarlinkStoreV0_1:
    def __init__(
        self,
        blobs: StarlinkBlobStore,
        catalog: StarlinkCatalogV0_1,
    ) -> None:
        self._blobs = blobs
        self._catalog = catalog

    def publish(
        self,
        request: StarlinkPilotAnalysisRequestV0_1,
        bundle: StarlinkPilotAnalysisBundleV0_1,
        *,
        idempotency_key: str,
    ) -> StarlinkPilotAnalysisProductRefV0_1:
        projection = starlink_projection_v0_1(request, bundle)
        payload = encode_starlink_bundle(bundle)
        bundle_ref = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=STARLINK_MEDIA_TYPE,
            format_id=STARLINK_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:starlink-bundle-v0.1",
        )
        return self._catalog.publish_starlink(
            projection,
            bundle_ref,
            request.recording_object_ref,
            idempotency_key=idempotency_key,
        )

    def open(
        self, ref: StarlinkPilotAnalysisProductRefV0_1
    ) -> AbstractContextManager[DurableStarlinkViewV0_1]:
        return self._open(ref)

    @contextmanager
    def _open(
        self, ref: StarlinkPilotAnalysisProductRefV0_1
    ) -> Iterator[DurableStarlinkViewV0_1]:
        cataloged = self._catalog.get_starlink(ref)
        if cataloged is None:
            raise StarlinkNotFoundError(
                "no Starlink result exactly matches the reference"
            )
        bundle_ref = cataloged.bundle_ref
        if (
            bundle_ref != ref.bundle_ref
            or bundle_ref.media_type != STARLINK_MEDIA_TYPE
            or bundle_ref.format_id != STARLINK_FORMAT_ID
            or bundle_ref.byte_count > MAX_STARLINK_BUNDLE_BYTES
        ):
            raise StarlinkIntegrityError("Starlink bundle metadata is invalid")
        metadata = self._blobs.head(bundle_ref)
        if metadata.ref != bundle_ref or not metadata.verified:
            raise StarlinkIntegrityError("blob store did not verify Starlink metadata")
        with self._blobs.open(bundle_ref) as stream:
            payload = stream.read(MAX_STARLINK_BUNDLE_BYTES + 1)
        if (
            len(payload) != bundle_ref.byte_count
            or Digest.sha256(payload) != bundle_ref.digest
        ):
            raise StarlinkIntegrityError("Starlink bytes do not match catalog metadata")
        try:
            bundle = decode_starlink_bundle(payload)
        except ValueError as error:
            raise StarlinkIntegrityError("Starlink bundle bytes are invalid") from error
        if (
            bundle.analysis_id != ref.analysis_id
            or bundle.recording_id != ref.recording_id
            or _bundle_projection(bundle, cataloged.projection.request_digest)
            != cataloged.projection
        ):
            raise StarlinkIntegrityError("Starlink bundle disagrees with catalog")
        yield DurableStarlinkViewV0_1(ref, bundle)


class StarlinkBlobStore(BlobReader, BlobWriter, Protocol):
    pass


def starlink_projection_v0_1(
    request: StarlinkPilotAnalysisRequestV0_1,
    bundle: StarlinkPilotAnalysisBundleV0_1,
) -> StarlinkCatalogProjectionV0_1:
    if request.requested_output_schema != bundle.schema:
        raise StarlinkIntegrityError("request does not select the bundle schema")
    recording_digest = request.recording_object_ref.identity_digest()
    if bundle.recording_id != request.recording_id:
        raise StarlinkIntegrityError("Starlink bundle belongs to another recording")
    if bundle.recording_identity_digest != recording_digest:
        raise StarlinkIntegrityError("Starlink recording identity differs")
    if not bundle.candidates:
        raise StarlinkIntegrityError("candidate analysis must contain selected streams")
    selected = {
        (item.segment_id, item.receiver_chain_id): item
        for item in request.stream_selections
    }
    candidates = {
        (item.segment_id, item.receiver_chain_id): item for item in bundle.candidates
    }
    if set(candidates) != set(selected):
        raise StarlinkIntegrityError("candidate streams differ from exact request")
    for key, candidate in candidates.items():
        selection = selected[key]
        if (
            candidate.edge is not selection.edge
            or candidate.algorithm_ref != request.algorithm_ref
            or candidate.config_ref != request.config_ref
            or candidate.exact_template_ref != selection.exact_template_ref
            or candidate.conditioned_control_template_ref
            != selection.conditioned_control_template_ref
            or candidate.probe_sample_count != selection.probe_sample_count
        ):
            raise StarlinkIntegrityError(
                "candidate search identity differs from request"
            )
        if candidate.provenance.input_digests != (
            canonical_digest(
                {
                    "recording_identity_digest": str(recording_digest),
                    "segment_id": str(candidate.segment_id),
                    "receiver_chain_id": str(candidate.receiver_chain_id),
                }
            ),
        ):
            raise StarlinkIntegrityError("candidate input provenance differs")
    return _bundle_projection(bundle, canonical_digest(request))


def _bundle_projection(
    bundle: StarlinkPilotAnalysisBundleV0_1, request_digest: Digest
) -> StarlinkCatalogProjectionV0_1:
    streams = {(item.segment_id, item.receiver_chain_id) for item in bundle.candidates}
    return StarlinkCatalogProjectionV0_1(
        bundle.analysis_id,
        str(bundle.recording_id),
        bundle.recording_identity_digest,
        request_digest,
        len(bundle.candidates),
        len(streams),
    )
