"""Integrity closure, durable store, and bounded acquired-QAM v0.3 query."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import Digest, RecordingId, SchemaRef
from leo_flow.contracts.starlink_acquired_constellation_pipeline import (
    RecordingReceiverLnbResolverV0_3,
    RecordingStarlinkAcquiredConstellationViewV0_3,
    StarlinkAcquiredConstellationCatalogProjectionV0_3,
    StarlinkAcquiredConstellationPresentationStreamV0_3,
    StarlinkAcquiredConstellationProductRefV0_3,
    StarlinkAcquiredConstellationQueryV0_3,
    StarlinkAcquiredConstellationRecordingBundleV0_3,
    StarlinkAcquiredConstellationRequestV0_3,
    StarlinkAcquiredConstellationViewMode,
    acquired_constellation_presentation_window,
)
from leo_flow.contracts.starlink_acquisition import V0_3
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .starlink_acquired_constellation_recording_codec import (
    MAX_STARLINK_ACQUIRED_CONSTELLATION_RECORDING_BYTES,
    STARLINK_ACQUIRED_CONSTELLATION_RECORDING_FORMAT_ID,
    STARLINK_ACQUIRED_CONSTELLATION_RECORDING_MEDIA_TYPE,
    decode_starlink_acquired_constellation_recording,
    encode_starlink_acquired_constellation_recording,
)


class StarlinkAcquiredConstellationIntegrityError(RuntimeError):
    pass


class StarlinkAcquiredConstellationNotFoundError(LookupError):
    pass


class StarlinkAcquiredConstellationConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogedStarlinkAcquiredConstellationV0_3:
    projection: StarlinkAcquiredConstellationCatalogProjectionV0_3
    bundle_ref: ObjectRef

    @property
    def ref(self) -> StarlinkAcquiredConstellationProductRefV0_3:
        return StarlinkAcquiredConstellationProductRefV0_3(
            self.projection.analysis_id, self.projection.recording_id, self.bundle_ref
        )


class StarlinkAcquiredConstellationCatalogV0_3(Protocol):
    def publish_starlink_acquired_constellation(
        self,
        projection: StarlinkAcquiredConstellationCatalogProjectionV0_3,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkAcquiredConstellationProductRefV0_3: ...
    def get_starlink_acquired_constellation(
        self, ref: StarlinkAcquiredConstellationProductRefV0_3
    ) -> CatalogedStarlinkAcquiredConstellationV0_3 | None: ...
    def latest_starlink_acquired_constellation(
        self, recording_id: RecordingId
    ) -> StarlinkAcquiredConstellationProductRefV0_3 | None: ...


class StarlinkAcquiredConstellationBlobStore(BlobReader, BlobWriter, Protocol):
    pass


class DurableStarlinkAcquiredConstellationStoreV0_3:
    def __init__(
        self,
        blobs: StarlinkAcquiredConstellationBlobStore,
        catalog: StarlinkAcquiredConstellationCatalogV0_3,
    ) -> None:
        self._blobs, self._catalog = blobs, catalog

    def publish(
        self,
        request: StarlinkAcquiredConstellationRequestV0_3,
        bundle: StarlinkAcquiredConstellationRecordingBundleV0_3,
        *,
        idempotency_key: str,
    ) -> StarlinkAcquiredConstellationProductRefV0_3:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        projection = starlink_acquired_constellation_projection_v0_3(request, bundle)
        payload = encode_starlink_acquired_constellation_recording(bundle)
        blob = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=STARLINK_ACQUIRED_CONSTELLATION_RECORDING_MEDIA_TYPE,
            format_id=STARLINK_ACQUIRED_CONSTELLATION_RECORDING_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:bundle-v0.3",
        )
        expected = StarlinkAcquiredConstellationProductRefV0_3(
            bundle.analysis_id, bundle.recording_id, blob
        )
        actual = self._catalog.publish_starlink_acquired_constellation(
            projection,
            blob,
            request.recording_object_ref,
            idempotency_key=idempotency_key,
        )
        if actual != expected:
            raise StarlinkAcquiredConstellationConflictError(
                "catalog replay returned another acquired-QAM product"
            )
        return actual

    def open(
        self, ref: StarlinkAcquiredConstellationProductRefV0_3
    ) -> AbstractContextManager[StarlinkAcquiredConstellationRecordingBundleV0_3]:
        return self._open(ref)

    @contextmanager
    def _open(
        self, ref: StarlinkAcquiredConstellationProductRefV0_3
    ) -> Iterator[StarlinkAcquiredConstellationRecordingBundleV0_3]:
        cataloged = self._catalog.get_starlink_acquired_constellation(ref)
        if cataloged is None:
            raise StarlinkAcquiredConstellationNotFoundError(
                "acquired-QAM product was not found"
            )
        blob = cataloged.bundle_ref
        if (
            cataloged.ref != ref
            or blob.media_type != STARLINK_ACQUIRED_CONSTELLATION_RECORDING_MEDIA_TYPE
            or blob.format_id != STARLINK_ACQUIRED_CONSTELLATION_RECORDING_FORMAT_ID
            or blob.byte_count > MAX_STARLINK_ACQUIRED_CONSTELLATION_RECORDING_BYTES
        ):
            raise StarlinkAcquiredConstellationIntegrityError(
                "acquired-QAM bundle metadata is invalid"
            )
        metadata = self._blobs.head(blob)
        if metadata.ref != blob or not metadata.verified:
            raise StarlinkAcquiredConstellationIntegrityError(
                "acquired-QAM blob is not verified"
            )
        with self._blobs.open(blob) as stream:
            payload = stream.read(
                MAX_STARLINK_ACQUIRED_CONSTELLATION_RECORDING_BYTES + 1
            )
        if len(payload) != blob.byte_count or Digest.sha256(payload) != blob.digest:
            raise StarlinkAcquiredConstellationIntegrityError(
                "acquired-QAM bytes differ"
            )
        try:
            bundle = decode_starlink_acquired_constellation_recording(payload)
        except ValueError as error:
            raise StarlinkAcquiredConstellationIntegrityError(
                "acquired-QAM bundle is invalid"
            ) from error
        if (
            bundle.analysis_id != ref.analysis_id
            or bundle.recording_id != ref.recording_id
            or _projection(bundle) != cataloged.projection
        ):
            raise StarlinkAcquiredConstellationIntegrityError(
                "acquired-QAM catalog and bundle differ"
            )
        yield bundle


class DurableRecordingStarlinkAcquiredConstellationQueryV0_3:
    def __init__(
        self,
        store: DurableStarlinkAcquiredConstellationStoreV0_3,
        catalog: StarlinkAcquiredConstellationCatalogV0_3,
        lnb_resolver: RecordingReceiverLnbResolverV0_3,
    ) -> None:
        self._store, self._catalog, self._lnb_resolver = store, catalog, lnb_resolver

    def recording_starlink_acquired_constellation(
        self, query: StarlinkAcquiredConstellationQueryV0_3
    ) -> RecordingStarlinkAcquiredConstellationViewV0_3:
        ref = self._catalog.latest_starlink_acquired_constellation(query.recording_id)
        if ref is None:
            raise StarlinkAcquiredConstellationNotFoundError(
                "recording has no acquired-QAM product"
            )
        with self._store.open(ref) as bundle:
            selected: list[StarlinkAcquiredConstellationPresentationStreamV0_3] = []
            truncated = False
            for stream in bundle.streams:
                lnb_id = self._lnb_resolver.lnb_id_for_recording_receiver(
                    query.recording_id, stream.receiver_chain_id
                )
                if (
                    (query.radio_ids and stream.radio_id not in query.radio_ids)
                    or (query.lnb_ids and lnb_id not in query.lnb_ids)
                    or (
                        query.segment_ids and stream.segment_id not in query.segment_ids
                    )
                    or (
                        query.receiver_chain_ids
                        and stream.receiver_chain_id not in query.receiver_chain_ids
                    )
                    or (query.edges and stream.edge not in query.edges)
                ):
                    continue
                windows = stream.windows
                if query.mode is StarlinkAcquiredConstellationViewMode.OVERALL:
                    windows = (windows[stream.overall.selected_display_window_index],)
                elif len(windows) > query.maximum_windows_per_stream:
                    windows = windows[: query.maximum_windows_per_stream]
                    truncated = True
                selected.append(
                    StarlinkAcquiredConstellationPresentationStreamV0_3(
                        stream.radio_id,
                        lnb_id,
                        stream.segment_id,
                        stream.receiver_chain_id,
                        stream.edge,
                        stream.sample_rate_hz,
                        stream.segment_sample_count,
                        stream.overall,
                        tuple(
                            acquired_constellation_presentation_window(
                                item, query.maximum_points_per_constellation
                            )
                            for item in windows
                        ),
                        len(stream.windows),
                    )
                )
            if len(selected) > query.maximum_streams:
                truncated = True
            return RecordingStarlinkAcquiredConstellationViewV0_3(
                SchemaRef(
                    RecordingStarlinkAcquiredConstellationViewV0_3.SCHEMA_ID, V0_3
                ),
                query.recording_id,
                ref.artifact_ref,
                bundle.source_suite_ref,
                query.mode,
                tuple(selected[: query.maximum_streams]),
                truncated,
                True,
                True,
            )


def starlink_acquired_constellation_projection_v0_3(
    request: StarlinkAcquiredConstellationRequestV0_3,
    bundle: StarlinkAcquiredConstellationRecordingBundleV0_3,
) -> StarlinkAcquiredConstellationCatalogProjectionV0_3:
    if (
        request.requested_output_schema != bundle.schema
        or request.recording_id != bundle.recording_id
        or request.recording_object_ref.identity_digest()
        != bundle.recording_identity_digest
        or request.source_suite_ref != bundle.source_suite_ref
        or request.source_suite_request_digest != bundle.source_suite_request_digest
        or request.digest != bundle.request_digest
    ):
        raise StarlinkAcquiredConstellationIntegrityError(
            "acquired-QAM request and bundle differ"
        )
    keys = tuple(
        (item.radio_id, item.segment_id, item.receiver_chain_id, item.edge)
        for item in bundle.streams
    )
    if keys != request.stream_keys or any(
        len(item.windows) > request.maximum_windows_per_stream
        for item in bundle.streams
    ):
        raise StarlinkAcquiredConstellationIntegrityError(
            "acquired-QAM stream/window membership differs"
        )
    return _projection(bundle)


def _projection(
    bundle: StarlinkAcquiredConstellationRecordingBundleV0_3,
) -> StarlinkAcquiredConstellationCatalogProjectionV0_3:
    windows = sum(len(item.windows) for item in bundle.streams)
    return StarlinkAcquiredConstellationCatalogProjectionV0_3(
        bundle.analysis_id,
        bundle.recording_id,
        bundle.recording_identity_digest,
        bundle.source_suite_ref,
        bundle.source_suite_request_digest,
        bundle.request_digest,
        len(bundle.streams),
        windows,
        windows * 2400,
        True,
    )
