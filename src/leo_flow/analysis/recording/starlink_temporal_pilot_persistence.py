"""Integrity closure, durable store and bounded temporal dashboard projection."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import ArtifactRef, Digest, RecordingId, SchemaRef
from leo_flow.contracts.starlink_temporal_pilot import (
    V0_1,
    RecordingStarlinkTemporalPilotViewV0_1,
    StarlinkTemporalMethodPointV0_1,
    StarlinkTemporalPilotCatalogProjectionV0_1,
    StarlinkTemporalPilotProductRefV0_1,
    StarlinkTemporalPilotQueryV0_1,
    StarlinkTemporalPilotRecordingBundleV0_1,
    StarlinkTemporalPilotRequestV0_1,
    StarlinkTemporalPresentationStreamV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .starlink_temporal_pilot_codec import (
    MAX_STARLINK_TEMPORAL_PILOT_BYTES,
    STARLINK_TEMPORAL_PILOT_FORMAT_ID,
    STARLINK_TEMPORAL_PILOT_MEDIA_TYPE,
    decode_starlink_temporal_pilot,
    encode_starlink_temporal_pilot,
)


class StarlinkTemporalPilotIntegrityError(RuntimeError):
    pass


class StarlinkTemporalPilotNotFoundError(LookupError):
    pass


class StarlinkTemporalPilotConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogedStarlinkTemporalPilotV0_1:
    projection: StarlinkTemporalPilotCatalogProjectionV0_1
    bundle_ref: ObjectRef

    @property
    def ref(self) -> StarlinkTemporalPilotProductRefV0_1:
        return StarlinkTemporalPilotProductRefV0_1(
            self.projection.analysis_id, self.projection.recording_id, self.bundle_ref
        )


class StarlinkTemporalPilotCatalogV0_1(Protocol):
    def publish_starlink_temporal_pilot(
        self,
        projection: StarlinkTemporalPilotCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkTemporalPilotProductRefV0_1: ...

    def get_starlink_temporal_pilot(
        self, ref: StarlinkTemporalPilotProductRefV0_1
    ) -> CatalogedStarlinkTemporalPilotV0_1 | None: ...

    def latest_starlink_temporal_pilot(
        self, recording_id: RecordingId
    ) -> StarlinkTemporalPilotProductRefV0_1 | None: ...


class StarlinkTemporalPilotBlobStore(BlobReader, BlobWriter, Protocol):
    pass


class DurableStarlinkTemporalPilotStoreV0_1:
    def __init__(
        self,
        blobs: StarlinkTemporalPilotBlobStore,
        catalog: StarlinkTemporalPilotCatalogV0_1,
    ) -> None:
        self._blobs, self._catalog = blobs, catalog

    def publish(
        self,
        request: StarlinkTemporalPilotRequestV0_1,
        bundle: StarlinkTemporalPilotRecordingBundleV0_1,
        *,
        idempotency_key: str,
    ) -> StarlinkTemporalPilotProductRefV0_1:
        projection = starlink_temporal_pilot_projection_v0_1(request, bundle)
        payload = encode_starlink_temporal_pilot(bundle)
        ref = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=STARLINK_TEMPORAL_PILOT_MEDIA_TYPE,
            format_id=STARLINK_TEMPORAL_PILOT_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:bundle-v0.1",
        )
        return self._catalog.publish_starlink_temporal_pilot(
            projection,
            ref,
            request.recording_object_ref,
            idempotency_key=idempotency_key,
        )

    def open(
        self, ref: StarlinkTemporalPilotProductRefV0_1
    ) -> AbstractContextManager[StarlinkTemporalPilotRecordingBundleV0_1]:
        return self._open(ref)

    @contextmanager
    def _open(
        self, ref: StarlinkTemporalPilotProductRefV0_1
    ) -> Iterator[StarlinkTemporalPilotRecordingBundleV0_1]:
        cataloged = self._catalog.get_starlink_temporal_pilot(ref)
        if cataloged is None or cataloged.ref != ref:
            raise StarlinkTemporalPilotNotFoundError("temporal product was not found")
        blob = cataloged.bundle_ref
        if (
            blob.media_type != STARLINK_TEMPORAL_PILOT_MEDIA_TYPE
            or blob.format_id != STARLINK_TEMPORAL_PILOT_FORMAT_ID
            or blob.byte_count > MAX_STARLINK_TEMPORAL_PILOT_BYTES
        ):
            raise StarlinkTemporalPilotIntegrityError("temporal metadata is invalid")
        metadata = self._blobs.head(blob)
        if metadata.ref != blob or not metadata.verified:
            raise StarlinkTemporalPilotIntegrityError("temporal blob is not verified")
        with self._blobs.open(blob) as stream:
            payload = stream.read(MAX_STARLINK_TEMPORAL_PILOT_BYTES + 1)
        if len(payload) != blob.byte_count or Digest.sha256(payload) != blob.digest:
            raise StarlinkTemporalPilotIntegrityError("temporal bytes differ")
        try:
            bundle = decode_starlink_temporal_pilot(payload)
        except ValueError as error:
            raise StarlinkTemporalPilotIntegrityError(
                "temporal bundle is invalid"
            ) from error
        if (
            bundle.analysis_id != ref.analysis_id
            or bundle.recording_id != ref.recording_id
            or _projection(bundle) != cataloged.projection
        ):
            raise StarlinkTemporalPilotIntegrityError(
                "temporal catalog and bundle differ"
            )
        yield bundle


class DurableRecordingStarlinkTemporalPilotQueryV0_1:
    def __init__(
        self,
        store: DurableStarlinkTemporalPilotStoreV0_1,
        catalog: StarlinkTemporalPilotCatalogV0_1,
    ) -> None:
        self._store, self._catalog = store, catalog

    def recording_starlink_temporal_pilot(
        self, query: StarlinkTemporalPilotQueryV0_1
    ) -> RecordingStarlinkTemporalPilotViewV0_1:
        ref = self._catalog.latest_starlink_temporal_pilot(query.recording_id)
        if ref is None:
            raise StarlinkTemporalPilotNotFoundError(
                "recording has no temporal product"
            )
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
            presentations: list[StarlinkTemporalPresentationStreamV0_1] = []
            for index, (stream, points) in enumerate(filtered):
                stream_count_left = len(filtered) - index
                budget = max(1, remaining // stream_count_left)
                display = _extrema_preserving(points, budget)
                remaining -= len(display)
                presentations.append(
                    StarlinkTemporalPresentationStreamV0_1(
                        stream.radio_id,
                        stream.segment_id,
                        stream.receiver_chain_id,
                        stream.channel_number,
                        stream.edge,
                        stream.sample_rate_hz,
                        stream.segment_sample_count,
                        stream.analyzed_sample_count,
                        stream.coverage_fraction,
                        display,
                        tuple(
                            item
                            for item in stream.dwell_summaries
                            if item.method in query.methods
                        ),
                    )
                )
        shown = sum(len(item.points) for item in presentations)
        return RecordingStarlinkTemporalPilotViewV0_1(
            SchemaRef(RecordingStarlinkTemporalPilotViewV0_1.SCHEMA_ID, V0_1),
            bundle.recording_id,
            ArtifactRef(bundle.analysis_id, ref.bundle_ref.digest, bundle.schema),
            bundle.plan,
            tuple(presentations),
            original,
            shown < original,
            "none"
            if shown == original
            else "per-stream-time-bucket-min-max-preserving",
            bundle.warnings,
        )


def starlink_temporal_pilot_projection_v0_1(
    request: StarlinkTemporalPilotRequestV0_1,
    bundle: StarlinkTemporalPilotRecordingBundleV0_1,
) -> StarlinkTemporalPilotCatalogProjectionV0_1:
    if (
        request.recording_id != bundle.recording_id
        or request.recording_object_ref.identity_digest()
        != bundle.recording_identity_digest
        or request.digest != bundle.request_digest
        or request.source_suite_ref != bundle.source_suite_ref
        or request.search_grid != bundle.search_grid
        or request.plan != bundle.plan
    ):
        raise StarlinkTemporalPilotIntegrityError("temporal request and bundle differ")
    return _projection(bundle)


def _projection(
    bundle: StarlinkTemporalPilotRecordingBundleV0_1,
) -> StarlinkTemporalPilotCatalogProjectionV0_1:
    return StarlinkTemporalPilotCatalogProjectionV0_1(
        bundle.analysis_id,
        bundle.recording_id,
        bundle.recording_identity_digest,
        bundle.request_digest,
        bundle.source_suite_ref,
        bundle.source_suite_request_digest,
        len(bundle.streams),
        sum(len(item.probe_starts) for item in bundle.streams),
        sum(len(item.points) for item in bundle.streams),
    )


def _extrema_preserving(
    points: tuple[StarlinkTemporalMethodPointV0_1, ...], maximum: int
) -> tuple[StarlinkTemporalMethodPointV0_1, ...]:
    if len(points) <= maximum:
        return points
    if maximum == 1:
        return (max(points, key=lambda item: item.qin.score),)
    bucket_count = max(1, maximum // 2)
    selected: set[int] = {0, len(points) - 1}
    for bucket in range(bucket_count):
        start = bucket * len(points) // bucket_count
        stop = (bucket + 1) * len(points) // bucket_count
        indices = range(start, stop)
        selected.add(min(indices, key=lambda index: points[index].qin.score))
        selected.add(max(indices, key=lambda index: points[index].qin.score))
    ordered = sorted(selected)
    if len(ordered) > maximum:
        ordered = ordered[: maximum - 1] + [ordered[-1]]
    return tuple(points[index] for index in ordered)
