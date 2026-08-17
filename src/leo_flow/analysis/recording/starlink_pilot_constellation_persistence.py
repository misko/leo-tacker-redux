"""Integrity closure, durable store, and bounded constellation query."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import Digest, RecordingId, SchemaRef
from leo_flow.contracts.starlink_pilot_constellation_pipeline import (
    RecordingStarlinkPilotConstellationViewV0_1,
    StarlinkPilotConstellationCatalogProjectionV0_1,
    StarlinkPilotConstellationProductRefV0_1,
    StarlinkPilotConstellationQueryV0_1,
    StarlinkPilotConstellationRecordingBundleV0_1,
    StarlinkPilotConstellationRequestV0_1,
    constellation_presentation_stream,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .starlink_pilot_constellation_recording_codec import (
    MAX_STARLINK_PILOT_CONSTELLATION_RECORDING_BYTES,
    STARLINK_PILOT_CONSTELLATION_RECORDING_FORMAT_ID,
    STARLINK_PILOT_CONSTELLATION_RECORDING_MEDIA_TYPE,
    decode_starlink_pilot_constellation_recording,
    encode_starlink_pilot_constellation_recording,
)


class StarlinkPilotConstellationIntegrityError(RuntimeError):
    pass


class StarlinkPilotConstellationNotFoundError(LookupError):
    pass


class StarlinkPilotConstellationConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogedStarlinkPilotConstellationV0_1:
    projection: StarlinkPilotConstellationCatalogProjectionV0_1
    bundle_ref: ObjectRef

    @property
    def ref(self) -> StarlinkPilotConstellationProductRefV0_1:
        return StarlinkPilotConstellationProductRefV0_1(
            self.projection.analysis_id, self.projection.recording_id, self.bundle_ref
        )


class StarlinkPilotConstellationCatalogV0_1(Protocol):
    def publish_starlink_pilot_constellation(
        self,
        projection: StarlinkPilotConstellationCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkPilotConstellationProductRefV0_1: ...
    def get_starlink_pilot_constellation(
        self, ref: StarlinkPilotConstellationProductRefV0_1
    ) -> CatalogedStarlinkPilotConstellationV0_1 | None: ...
    def latest_starlink_pilot_constellation(
        self, recording_id: RecordingId
    ) -> StarlinkPilotConstellationProductRefV0_1 | None: ...


class StarlinkPilotConstellationBlobStore(BlobReader, BlobWriter, Protocol):
    pass


class DurableStarlinkPilotConstellationStoreV0_1:
    def __init__(
        self,
        blobs: StarlinkPilotConstellationBlobStore,
        catalog: StarlinkPilotConstellationCatalogV0_1,
    ) -> None:
        self._blobs, self._catalog = blobs, catalog

    def publish(
        self,
        request: StarlinkPilotConstellationRequestV0_1,
        bundle: StarlinkPilotConstellationRecordingBundleV0_1,
        *,
        idempotency_key: str,
    ) -> StarlinkPilotConstellationProductRefV0_1:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        projection = starlink_pilot_constellation_projection_v0_1(request, bundle)
        payload = encode_starlink_pilot_constellation_recording(bundle)
        blob = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=STARLINK_PILOT_CONSTELLATION_RECORDING_MEDIA_TYPE,
            format_id=STARLINK_PILOT_CONSTELLATION_RECORDING_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:bundle-v0.1",
        )
        expected = StarlinkPilotConstellationProductRefV0_1(
            bundle.analysis_id, bundle.recording_id, blob
        )
        actual = self._catalog.publish_starlink_pilot_constellation(
            projection,
            blob,
            request.recording_object_ref,
            idempotency_key=idempotency_key,
        )
        if actual != expected:
            raise StarlinkPilotConstellationConflictError(
                "catalog replay returned another constellation product"
            )
        return actual

    def open(
        self, ref: StarlinkPilotConstellationProductRefV0_1
    ) -> AbstractContextManager[StarlinkPilotConstellationRecordingBundleV0_1]:
        return self._open(ref)

    @contextmanager
    def _open(
        self, ref: StarlinkPilotConstellationProductRefV0_1
    ) -> Iterator[StarlinkPilotConstellationRecordingBundleV0_1]:
        cataloged = self._catalog.get_starlink_pilot_constellation(ref)
        if cataloged is None:
            raise StarlinkPilotConstellationNotFoundError(
                "constellation product was not found"
            )
        blob = cataloged.bundle_ref
        if (
            cataloged.ref != ref
            or blob.media_type != STARLINK_PILOT_CONSTELLATION_RECORDING_MEDIA_TYPE
            or blob.format_id != STARLINK_PILOT_CONSTELLATION_RECORDING_FORMAT_ID
            or blob.byte_count > MAX_STARLINK_PILOT_CONSTELLATION_RECORDING_BYTES
        ):
            raise StarlinkPilotConstellationIntegrityError(
                "constellation bundle metadata is invalid"
            )
        metadata = self._blobs.head(blob)
        if metadata.ref != blob or not metadata.verified:
            raise StarlinkPilotConstellationIntegrityError(
                "constellation blob is not verified"
            )
        with self._blobs.open(blob) as stream:
            payload = stream.read(MAX_STARLINK_PILOT_CONSTELLATION_RECORDING_BYTES + 1)
        if len(payload) != blob.byte_count or Digest.sha256(payload) != blob.digest:
            raise StarlinkPilotConstellationIntegrityError("constellation bytes differ")
        try:
            bundle = decode_starlink_pilot_constellation_recording(payload)
        except ValueError as error:
            raise StarlinkPilotConstellationIntegrityError(
                "constellation bundle is invalid"
            ) from error
        if (
            bundle.analysis_id != ref.analysis_id
            or bundle.recording_id != ref.recording_id
            or _projection(bundle) != cataloged.projection
        ):
            raise StarlinkPilotConstellationIntegrityError(
                "constellation catalog and bundle differ"
            )
        yield bundle


class DurableRecordingStarlinkPilotConstellationQueryV0_1:
    def __init__(
        self,
        store: DurableStarlinkPilotConstellationStoreV0_1,
        catalog: StarlinkPilotConstellationCatalogV0_1,
    ) -> None:
        self._store, self._catalog = store, catalog

    def recording_starlink_pilot_constellation(
        self, query: StarlinkPilotConstellationQueryV0_1
    ) -> RecordingStarlinkPilotConstellationViewV0_1:
        ref = self._catalog.latest_starlink_pilot_constellation(query.recording_id)
        if ref is None:
            raise StarlinkPilotConstellationNotFoundError(
                "recording has no constellation product"
            )
        with self._store.open(ref) as bundle:
            selected = [
                item
                for item in bundle.streams
                if (not query.segment_ids or item.segment_id in query.segment_ids)
                and (
                    not query.receiver_chain_ids
                    or item.receiver_chain_id in query.receiver_chain_ids
                )
                and (not query.edges or item.edge in query.edges)
            ]
            truncated = len(selected) > query.maximum_streams or any(
                len(item.points) > query.maximum_points_per_stream for item in selected
            )
            streams = tuple(
                constellation_presentation_stream(item, query.maximum_points_per_stream)
                for item in selected[: query.maximum_streams]
            )
            return RecordingStarlinkPilotConstellationViewV0_1(
                SchemaRef(RecordingStarlinkPilotConstellationViewV0_1.SCHEMA_ID),
                query.recording_id,
                ref.artifact_ref,
                bundle.source_suite_ref,
                streams,
                truncated,
            )


def starlink_pilot_constellation_projection_v0_1(
    request: StarlinkPilotConstellationRequestV0_1,
    bundle: StarlinkPilotConstellationRecordingBundleV0_1,
) -> StarlinkPilotConstellationCatalogProjectionV0_1:
    if (
        request.requested_output_schema != bundle.schema
        or request.recording_id != bundle.recording_id
        or request.recording_object_ref.identity_digest()
        != bundle.recording_identity_digest
        or request.source_suite_ref != bundle.source_suite_ref
        or request.source_suite_request_digest != bundle.source_suite_request_digest
        or request.digest != bundle.request_digest
    ):
        raise StarlinkPilotConstellationIntegrityError(
            "constellation request and bundle differ"
        )
    keys = tuple(
        (item.segment_id, item.receiver_chain_id, item.edge) for item in bundle.streams
    )
    if keys != request.stream_keys:
        raise StarlinkPilotConstellationIntegrityError(
            "constellation stream membership differs"
        )
    return _projection(bundle)


def _projection(
    bundle: StarlinkPilotConstellationRecordingBundleV0_1,
) -> StarlinkPilotConstellationCatalogProjectionV0_1:
    return StarlinkPilotConstellationCatalogProjectionV0_1(
        bundle.analysis_id,
        bundle.recording_id,
        bundle.recording_identity_digest,
        bundle.source_suite_ref,
        bundle.source_suite_request_digest,
        bundle.request_digest,
        len(bundle.streams),
        sum(len(item.points) for item in bundle.streams),
    )
