"""CAS-first publication and bounded read model for full-dwell responses."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import ArtifactRef, Digest, RecordingId, SchemaRef
from leo_flow.contracts.starlink_full_dwell_response import (
    V0_1,
    RecordingStarlinkFullDwellViewV0_1,
    StarlinkFullDwellCatalogProjectionV0_1,
    StarlinkFullDwellPresentationStreamV0_1,
    StarlinkFullDwellProductRefV0_1,
    StarlinkFullDwellQueryV0_1,
    StarlinkFullDwellRequestV0_1,
    StarlinkFullDwellResponseBundleV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .starlink_full_dwell_response_codec import (
    MAX_STARLINK_FULL_DWELL_RESPONSE_BYTES,
    STARLINK_FULL_DWELL_RESPONSE_FORMAT_ID,
    STARLINK_FULL_DWELL_RESPONSE_MEDIA_TYPE,
    decode_starlink_full_dwell_response,
    encode_starlink_full_dwell_response,
)


class StarlinkFullDwellIntegrityError(RuntimeError):
    pass


class StarlinkFullDwellNotFoundError(LookupError):
    pass


class StarlinkFullDwellConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogedStarlinkFullDwellV0_1:
    projection: StarlinkFullDwellCatalogProjectionV0_1
    bundle_ref: ObjectRef

    @property
    def ref(self) -> StarlinkFullDwellProductRefV0_1:
        return StarlinkFullDwellProductRefV0_1(
            self.projection.analysis_id, self.projection.recording_id, self.bundle_ref
        )


class StarlinkFullDwellCatalogV0_1(Protocol):
    def publish_starlink_full_dwell(
        self,
        projection: StarlinkFullDwellCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
        bundle: StarlinkFullDwellResponseBundleV0_1,
    ) -> StarlinkFullDwellProductRefV0_1: ...

    def get_starlink_full_dwell(
        self, ref: StarlinkFullDwellProductRefV0_1
    ) -> CatalogedStarlinkFullDwellV0_1 | None: ...

    def latest_starlink_full_dwell(
        self, recording_id: RecordingId
    ) -> StarlinkFullDwellProductRefV0_1 | None: ...


class StarlinkFullDwellBlobStore(BlobReader, BlobWriter, Protocol):
    pass


class DurableStarlinkFullDwellStoreV0_1:
    def __init__(
        self, blobs: StarlinkFullDwellBlobStore, catalog: StarlinkFullDwellCatalogV0_1
    ) -> None:
        self._blobs, self._catalog = blobs, catalog

    def publish(
        self,
        request: StarlinkFullDwellRequestV0_1,
        bundle: StarlinkFullDwellResponseBundleV0_1,
        *,
        idempotency_key: str,
    ) -> StarlinkFullDwellProductRefV0_1:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        projection = starlink_full_dwell_projection_v0_1(request, bundle)
        payload = encode_starlink_full_dwell_response(bundle)
        blob = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=STARLINK_FULL_DWELL_RESPONSE_MEDIA_TYPE,
            format_id=STARLINK_FULL_DWELL_RESPONSE_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:bundle-v0.1",
        )
        expected = StarlinkFullDwellProductRefV0_1(
            bundle.analysis_id, bundle.recording_id, blob
        )
        actual = self._catalog.publish_starlink_full_dwell(
            projection,
            blob,
            request.recording_object_ref,
            idempotency_key=idempotency_key,
            bundle=bundle,
        )
        if actual != expected:
            raise StarlinkFullDwellConflictError(
                "catalog replay returned another full-dwell product"
            )
        return actual

    def open(
        self, ref: StarlinkFullDwellProductRefV0_1
    ) -> AbstractContextManager[StarlinkFullDwellResponseBundleV0_1]:
        return self._open(ref)

    @contextmanager
    def _open(
        self, ref: StarlinkFullDwellProductRefV0_1
    ) -> Iterator[StarlinkFullDwellResponseBundleV0_1]:
        cataloged = self._catalog.get_starlink_full_dwell(ref)
        if cataloged is None or cataloged.ref != ref:
            raise StarlinkFullDwellNotFoundError("full-dwell product was not found")
        blob = cataloged.bundle_ref
        if (
            blob.media_type != STARLINK_FULL_DWELL_RESPONSE_MEDIA_TYPE
            or blob.format_id != STARLINK_FULL_DWELL_RESPONSE_FORMAT_ID
            or blob.byte_count > MAX_STARLINK_FULL_DWELL_RESPONSE_BYTES
        ):
            raise StarlinkFullDwellIntegrityError("full-dwell metadata is invalid")
        metadata = self._blobs.head(blob)
        if metadata.ref != blob or not metadata.verified:
            raise StarlinkFullDwellIntegrityError("full-dwell blob is not verified")
        with self._blobs.open(blob) as stream:
            payload = stream.read(MAX_STARLINK_FULL_DWELL_RESPONSE_BYTES + 1)
        if len(payload) != blob.byte_count or Digest.sha256(payload) != blob.digest:
            raise StarlinkFullDwellIntegrityError("full-dwell bytes differ")
        try:
            bundle = decode_starlink_full_dwell_response(payload)
        except ValueError as error:
            raise StarlinkFullDwellIntegrityError(
                "full-dwell bundle is invalid"
            ) from error
        if (
            bundle.analysis_id != ref.analysis_id
            or bundle.recording_id != ref.recording_id
            or _projection(bundle) != cataloged.projection
        ):
            raise StarlinkFullDwellIntegrityError(
                "full-dwell catalog and bundle differ"
            )
        yield bundle


class DurableRecordingStarlinkFullDwellQueryV0_1:
    def __init__(
        self,
        store: DurableStarlinkFullDwellStoreV0_1,
        catalog: StarlinkFullDwellCatalogV0_1,
    ) -> None:
        self._store, self._catalog = store, catalog

    def recording_starlink_full_dwell(
        self, query: StarlinkFullDwellQueryV0_1
    ) -> RecordingStarlinkFullDwellViewV0_1:
        ref = self._catalog.latest_starlink_full_dwell(query.recording_id)
        if ref is None:
            raise StarlinkFullDwellNotFoundError("recording has no full-dwell product")
        with self._store.open(ref) as bundle:
            selected = tuple(
                stream
                for stream in bundle.streams
                if (not query.radio_ids or stream.radio_id in query.radio_ids)
                and (
                    not query.receiver_chain_ids
                    or stream.receiver_chain_id in query.receiver_chain_ids
                )
                and (not query.edges or stream.edge in query.edges)
            )
            filtered = tuple(
                (
                    stream,
                    tuple(
                        point
                        for point in stream.points
                        if point.method in query.methods
                    ),
                )
                for stream in selected
            )
            original = sum(len(points) for _stream, points in filtered)
            remaining = query.maximum_points
            views: list[StarlinkFullDwellPresentationStreamV0_1] = []
            for index, (stream, points) in enumerate(filtered):
                budget = max(1, remaining // (len(filtered) - index))
                shown = _bounded_time_points(points, budget)
                remaining -= len(shown)
                views.append(
                    StarlinkFullDwellPresentationStreamV0_1(
                        stream.radio_id,
                        stream.segment_id,
                        stream.receiver_chain_id,
                        stream.channel_number,
                        stream.edge,
                        stream.sample_rate_hz,
                        stream.segment_sample_count,
                        len(stream.prescreen_windows),
                        len(stream.exact_window_starts),
                        stream.prescreen_coverage_fraction,
                        stream.exact_coverage_fraction,
                        stream.refinement_is_data_adaptive,
                        shown,
                    )
                )
        shown_count = sum(len(stream.points) for stream in views)
        return RecordingStarlinkFullDwellViewV0_1(
            SchemaRef(RecordingStarlinkFullDwellViewV0_1.SCHEMA_ID, V0_1),
            bundle.recording_id,
            ArtifactRef(bundle.analysis_id, ref.bundle_ref.digest, bundle.schema),
            bundle.plan,
            tuple(views),
            original,
            shown_count < original,
            "none" if shown_count == original else "even-time-index-preserving",
            "truncated" if shown_count < original else "complete",
            0,
            bundle.warnings,
        )


def starlink_full_dwell_projection_v0_1(
    request: StarlinkFullDwellRequestV0_1,
    bundle: StarlinkFullDwellResponseBundleV0_1,
) -> StarlinkFullDwellCatalogProjectionV0_1:
    if (
        request.recording_id != bundle.recording_id
        or request.recording_object_ref.identity_digest()
        != bundle.recording_identity_digest
        or request.source_suite_ref != bundle.source_suite_ref
        or request.source_suite_request_digest != bundle.source_suite_request_digest
        or request.digest != bundle.request_digest
        or request.requested_output_schema != bundle.schema
    ):
        raise StarlinkFullDwellIntegrityError("full-dwell request and bundle differ")
    expected = {
        (item.segment_id, item.receiver_chain_id, item.edge)
        for item in request.stream_selections
    }
    actual = {
        (item.segment_id, item.receiver_chain_id, item.edge) for item in bundle.streams
    }
    if (
        expected != actual
        or request.plan != bundle.plan
        or request.search_grid != bundle.search_grid
    ):
        raise StarlinkFullDwellIntegrityError(
            "full-dwell stream or search plan differs"
        )
    return _projection(bundle)


def _projection(
    bundle: StarlinkFullDwellResponseBundleV0_1,
) -> StarlinkFullDwellCatalogProjectionV0_1:
    return StarlinkFullDwellCatalogProjectionV0_1(
        bundle.analysis_id,
        bundle.recording_id,
        bundle.recording_identity_digest,
        bundle.source_suite_ref,
        bundle.source_suite_request_digest,
        bundle.request_digest,
        len(bundle.streams),
        sum(len(stream.prescreen_windows) for stream in bundle.streams),
        sum(len(stream.exact_window_starts) for stream in bundle.streams),
        sum(len(stream.points) for stream in bundle.streams),
    )


def _bounded_time_points(points: tuple, maximum: int) -> tuple:
    if len(points) <= maximum:
        return points
    if maximum == 1:
        return (points[0],)
    indices = tuple(
        round(index * (len(points) - 1) / (maximum - 1)) for index in range(maximum)
    )
    return tuple(points[index] for index in indices)
