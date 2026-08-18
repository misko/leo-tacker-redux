"""CAS-first persistence and bounded query for adaptive pilot responses."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import ArtifactRef, Digest, RecordingId, SchemaRef
from leo_flow.contracts.starlink_adaptive_response import (
    V0_1,
    RecordingStarlinkAdaptiveResponseViewV0_1,
    StarlinkAdaptiveResponseBundleV0_1,
    StarlinkAdaptiveResponseCatalogProjectionV0_1,
    StarlinkAdaptiveResponsePointV0_1,
    StarlinkAdaptiveResponsePresentationStreamV0_1,
    StarlinkAdaptiveResponseProductRefV0_1,
    StarlinkAdaptiveResponseQueryV0_1,
    StarlinkAdaptiveResponseRequestV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .starlink_adaptive_response_codec import (
    MAX_STARLINK_ADAPTIVE_RESPONSE_BYTES,
    STARLINK_ADAPTIVE_RESPONSE_FORMAT_ID,
    STARLINK_ADAPTIVE_RESPONSE_MEDIA_TYPE,
    decode_starlink_adaptive_response,
    encode_starlink_adaptive_response,
)


class StarlinkAdaptiveResponseNotFoundError(LookupError):
    pass


class StarlinkAdaptiveResponseIntegrityError(RuntimeError):
    pass


class StarlinkAdaptiveResponseConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogedStarlinkAdaptiveResponseV0_1:
    projection: StarlinkAdaptiveResponseCatalogProjectionV0_1
    bundle_ref: ObjectRef

    @property
    def ref(self) -> StarlinkAdaptiveResponseProductRefV0_1:
        return StarlinkAdaptiveResponseProductRefV0_1(
            self.projection.analysis_id,
            self.projection.recording_id,
            self.bundle_ref,
        )


class StarlinkAdaptiveResponseCatalogV0_1(Protocol):
    def publish_starlink_adaptive_response(
        self,
        projection: StarlinkAdaptiveResponseCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkAdaptiveResponseProductRefV0_1: ...

    def get_starlink_adaptive_response(
        self, ref: StarlinkAdaptiveResponseProductRefV0_1
    ) -> CatalogedStarlinkAdaptiveResponseV0_1 | None: ...

    def latest_starlink_adaptive_response(
        self, recording_id: RecordingId
    ) -> StarlinkAdaptiveResponseProductRefV0_1 | None: ...


class StarlinkAdaptiveResponseBlobStore(BlobReader, BlobWriter, Protocol):
    pass


class DurableStarlinkAdaptiveResponseStoreV0_1:
    def __init__(
        self,
        blobs: StarlinkAdaptiveResponseBlobStore,
        catalog: StarlinkAdaptiveResponseCatalogV0_1,
    ) -> None:
        self._blobs, self._catalog = blobs, catalog

    def publish(
        self,
        request: StarlinkAdaptiveResponseRequestV0_1,
        bundle: StarlinkAdaptiveResponseBundleV0_1,
        *,
        idempotency_key: str,
    ) -> StarlinkAdaptiveResponseProductRefV0_1:
        if not idempotency_key:
            raise ValueError("adaptive response idempotency key cannot be empty")
        projection = starlink_adaptive_response_projection_v0_1(request, bundle)
        payload = encode_starlink_adaptive_response(bundle)
        blob = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=STARLINK_ADAPTIVE_RESPONSE_MEDIA_TYPE,
            format_id=STARLINK_ADAPTIVE_RESPONSE_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:bundle-v0.1",
        )
        expected = StarlinkAdaptiveResponseProductRefV0_1(
            bundle.analysis_id, bundle.recording_id, blob
        )
        actual = self._catalog.publish_starlink_adaptive_response(
            projection,
            blob,
            request.recording_object_ref,
            idempotency_key=idempotency_key,
        )
        if actual != expected:
            raise StarlinkAdaptiveResponseConflictError(
                "adaptive response catalog replay returned another product"
            )
        return actual

    def open(
        self, ref: StarlinkAdaptiveResponseProductRefV0_1
    ) -> AbstractContextManager[StarlinkAdaptiveResponseBundleV0_1]:
        return self._open(ref)

    @contextmanager
    def _open(
        self, ref: StarlinkAdaptiveResponseProductRefV0_1
    ) -> Iterator[StarlinkAdaptiveResponseBundleV0_1]:
        cataloged = self._catalog.get_starlink_adaptive_response(ref)
        if cataloged is None or cataloged.ref != ref:
            raise StarlinkAdaptiveResponseNotFoundError(
                "adaptive response was not found"
            )
        blob = cataloged.bundle_ref
        if (
            blob.media_type != STARLINK_ADAPTIVE_RESPONSE_MEDIA_TYPE
            or blob.format_id != STARLINK_ADAPTIVE_RESPONSE_FORMAT_ID
            or blob.byte_count > MAX_STARLINK_ADAPTIVE_RESPONSE_BYTES
        ):
            raise StarlinkAdaptiveResponseIntegrityError(
                "adaptive response metadata is invalid"
            )
        metadata = self._blobs.head(blob)
        if metadata.ref != blob or not metadata.verified:
            raise StarlinkAdaptiveResponseIntegrityError(
                "adaptive response blob is not verified"
            )
        with self._blobs.open(blob) as stream:
            payload = stream.read(MAX_STARLINK_ADAPTIVE_RESPONSE_BYTES + 1)
        if len(payload) != blob.byte_count or Digest.sha256(payload) != blob.digest:
            raise StarlinkAdaptiveResponseIntegrityError(
                "adaptive response bytes differ"
            )
        try:
            bundle = decode_starlink_adaptive_response(payload)
        except ValueError as error:
            raise StarlinkAdaptiveResponseIntegrityError(
                "adaptive response bundle is malformed"
            ) from error
        if (
            bundle.analysis_id != ref.analysis_id
            or bundle.recording_id != ref.recording_id
            or _projection(bundle) != cataloged.projection
        ):
            raise StarlinkAdaptiveResponseIntegrityError(
                "adaptive response catalog and bundle differ"
            )
        yield bundle


class DurableRecordingStarlinkAdaptiveResponseQueryV0_1:
    def __init__(
        self,
        store: DurableStarlinkAdaptiveResponseStoreV0_1,
        catalog: StarlinkAdaptiveResponseCatalogV0_1,
    ) -> None:
        self._store, self._catalog = store, catalog

    def recording_starlink_adaptive_response(
        self, query: StarlinkAdaptiveResponseQueryV0_1
    ) -> RecordingStarlinkAdaptiveResponseViewV0_1:
        ref = self._catalog.latest_starlink_adaptive_response(query.recording_id)
        if ref is None:
            raise StarlinkAdaptiveResponseNotFoundError(
                "recording has no adaptive response"
            )
        with self._store.open(ref) as bundle:
            selected = tuple(
                stream
                for stream in bundle.streams
                if (not query.radio_ids or stream.radio_id in query.radio_ids)
                and (not query.lnb_ids or stream.lnb_id in query.lnb_ids)
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
            views = []
            for index, (stream, points) in enumerate(filtered):
                budget = max(1, remaining // (len(filtered) - index))
                shown = _bounded_points(points, budget)
                remaining -= len(shown)
                views.append(
                    StarlinkAdaptiveResponsePresentationStreamV0_1(
                        stream.radio_id,
                        stream.lnb_id,
                        stream.segment_id,
                        stream.receiver_chain_id,
                        stream.channel_number,
                        stream.edge,
                        stream.sample_rate_hz,
                        stream.segment_sample_count,
                        stream.selection,
                        shown,
                        stream.exact_coverage_fraction,
                    )
                )
            shown_count = sum(len(item.points) for item in views)
            return RecordingStarlinkAdaptiveResponseViewV0_1(
                SchemaRef(RecordingStarlinkAdaptiveResponseViewV0_1.SCHEMA_ID, V0_1),
                bundle.recording_id,
                ArtifactRef(bundle.analysis_id, ref.bundle_ref.digest, bundle.schema),
                bundle.timeline_ref,
                bundle.plan,
                tuple(views),
                original,
                shown_count < original,
                "none" if shown_count == original else "extrema-and-even-time",
                True,
                True,
                bundle.warnings,
            )


def starlink_adaptive_response_projection_v0_1(
    request: StarlinkAdaptiveResponseRequestV0_1,
    bundle: StarlinkAdaptiveResponseBundleV0_1,
) -> StarlinkAdaptiveResponseCatalogProjectionV0_1:
    if (
        request.recording_id != bundle.recording_id
        or request.recording_object_ref.identity_digest()
        != bundle.recording_identity_digest
        or request.timeline_ref != bundle.timeline_ref
        or request.source_suite_ref != bundle.source_suite_ref
        or request.digest != bundle.request_digest
        or request.search_grid != bundle.search_grid
        or request.plan != bundle.plan
        or request.requested_output_schema != bundle.schema
    ):
        raise StarlinkAdaptiveResponseIntegrityError(
            "adaptive response request and bundle differ"
        )
    if {item.identity for item in request.streams} != {
        item.identity for item in bundle.streams
    }:
        raise StarlinkAdaptiveResponseIntegrityError(
            "adaptive response stream membership differs"
        )
    return _projection(bundle)


def _projection(
    bundle: StarlinkAdaptiveResponseBundleV0_1,
) -> StarlinkAdaptiveResponseCatalogProjectionV0_1:
    return StarlinkAdaptiveResponseCatalogProjectionV0_1(
        bundle.analysis_id,
        bundle.recording_id,
        bundle.recording_identity_digest,
        bundle.timeline_ref,
        bundle.source_suite_ref,
        bundle.request_digest,
        len(bundle.streams),
        sum(len(item.selection.exact_windows) for item in bundle.streams),
        sum(len(item.points) for item in bundle.streams),
    )


def _bounded_points(
    points: tuple[StarlinkAdaptiveResponsePointV0_1, ...], maximum: int
) -> tuple[StarlinkAdaptiveResponsePointV0_1, ...]:
    if len(points) <= maximum:
        return points
    if maximum == 1:
        return (max(points, key=_point_abs_margin),)
    extrema = {
        points.index(max(points, key=_point_qin_score)),
        points.index(min(points, key=_point_qin_score)),
        points.index(max(points, key=_point_abs_margin)),
    }
    remaining = maximum - len(extrema)
    if remaining > 0:
        extrema.update(
            round(index * (len(points) - 1) / max(1, remaining - 1))
            for index in range(remaining)
        )
    indexes = tuple(sorted(extrema))[:maximum]
    return tuple(points[index] for index in indexes)


def _point_qin_score(value: StarlinkAdaptiveResponsePointV0_1) -> float:
    return value.qin.score


def _point_abs_margin(value: StarlinkAdaptiveResponsePointV0_1) -> float:
    return abs(value.qin_minus_max_surrogate)
