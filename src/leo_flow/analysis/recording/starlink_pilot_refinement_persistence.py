"""CAS-first persistence and bounded queries for pilot-refinement evidence."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import V0_1, ArtifactRef, Digest, RecordingId, SchemaRef
from leo_flow.contracts.starlink_adaptive_response import (
    StarlinkAdaptiveResponsePointV0_1,
)
from leo_flow.contracts.starlink_pilot_refinement import (
    RecordingStarlinkPilotRefinementViewV0_1,
    StarlinkPilotRefinementBundleV0_1,
    StarlinkPilotRefinementCatalogProjectionV0_1,
    StarlinkPilotRefinementPresentationStreamV0_1,
    StarlinkPilotRefinementProductRefV0_1,
    StarlinkPilotRefinementQueryV0_1,
    StarlinkPilotRefinementRequestV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .starlink_pilot_refinement_codec import (
    MAX_STARLINK_PILOT_REFINEMENT_BYTES,
    STARLINK_PILOT_REFINEMENT_FORMAT_ID,
    STARLINK_PILOT_REFINEMENT_MEDIA_TYPE,
    decode_starlink_pilot_refinement,
    encode_starlink_pilot_refinement,
)


class StarlinkPilotRefinementIntegrityError(RuntimeError):
    pass


class StarlinkPilotRefinementConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CatalogedStarlinkPilotRefinementV0_1:
    projection: StarlinkPilotRefinementCatalogProjectionV0_1
    bundle_ref: ObjectRef

    @property
    def ref(self) -> StarlinkPilotRefinementProductRefV0_1:
        return StarlinkPilotRefinementProductRefV0_1(
            self.projection.analysis_id, self.projection.recording_id, self.bundle_ref
        )


class StarlinkPilotRefinementCatalogV0_1(Protocol):
    def publish_starlink_pilot_refinement(
        self,
        projection: StarlinkPilotRefinementCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkPilotRefinementProductRefV0_1: ...

    def get_starlink_pilot_refinement(
        self, ref: StarlinkPilotRefinementProductRefV0_1
    ) -> CatalogedStarlinkPilotRefinementV0_1 | None: ...

    def latest_starlink_pilot_refinement(
        self, recording_id: RecordingId
    ) -> StarlinkPilotRefinementProductRefV0_1 | None: ...


class StarlinkPilotRefinementBlobStore(BlobReader, BlobWriter, Protocol):
    pass


class DurableStarlinkPilotRefinementStoreV0_1:
    def __init__(
        self,
        blobs: StarlinkPilotRefinementBlobStore,
        catalog: StarlinkPilotRefinementCatalogV0_1,
    ) -> None:
        self._blobs, self._catalog = blobs, catalog

    def publish(
        self,
        request: StarlinkPilotRefinementRequestV0_1,
        bundle: StarlinkPilotRefinementBundleV0_1,
        *,
        idempotency_key: str,
    ) -> StarlinkPilotRefinementProductRefV0_1:
        if not idempotency_key:
            raise ValueError("pilot-refinement idempotency key cannot be empty")
        projection = starlink_pilot_refinement_projection_v0_1(request, bundle)
        payload = encode_starlink_pilot_refinement(bundle)
        blob = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=STARLINK_PILOT_REFINEMENT_MEDIA_TYPE,
            format_id=STARLINK_PILOT_REFINEMENT_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:bundle-v0.1",
        )
        expected = StarlinkPilotRefinementProductRefV0_1(
            bundle.analysis_id, bundle.recording_id, blob
        )
        actual = self._catalog.publish_starlink_pilot_refinement(
            projection,
            blob,
            request.recording_object_ref,
            idempotency_key=idempotency_key,
        )
        if actual != expected:
            raise StarlinkPilotRefinementConflictError(
                "pilot-refinement catalog returned another product"
            )
        return actual

    def open(
        self, ref: StarlinkPilotRefinementProductRefV0_1
    ) -> AbstractContextManager[StarlinkPilotRefinementBundleV0_1]:
        return self._open(ref)

    @contextmanager
    def _open(
        self, ref: StarlinkPilotRefinementProductRefV0_1
    ) -> Iterator[StarlinkPilotRefinementBundleV0_1]:
        cataloged = self._catalog.get_starlink_pilot_refinement(ref)
        if cataloged is None or cataloged.ref != ref:
            raise LookupError("pilot-refinement product was not found")
        blob = cataloged.bundle_ref
        if (
            blob.media_type != STARLINK_PILOT_REFINEMENT_MEDIA_TYPE
            or blob.format_id != STARLINK_PILOT_REFINEMENT_FORMAT_ID
            or blob.byte_count > MAX_STARLINK_PILOT_REFINEMENT_BYTES
        ):
            raise StarlinkPilotRefinementIntegrityError(
                "pilot-refinement blob metadata is invalid"
            )
        metadata = self._blobs.head(blob)
        if metadata.ref != blob or not metadata.verified:
            raise StarlinkPilotRefinementIntegrityError(
                "pilot-refinement blob is not verified"
            )
        with self._blobs.open(blob) as stream:
            payload = stream.read(MAX_STARLINK_PILOT_REFINEMENT_BYTES + 1)
        if len(payload) != blob.byte_count or Digest.sha256(payload) != blob.digest:
            raise StarlinkPilotRefinementIntegrityError(
                "pilot-refinement blob bytes differ"
            )
        try:
            bundle = decode_starlink_pilot_refinement(payload)
        except ValueError as error:
            raise StarlinkPilotRefinementIntegrityError(
                "pilot-refinement bundle is malformed"
            ) from error
        if _projection(bundle) != cataloged.projection:
            raise StarlinkPilotRefinementIntegrityError(
                "pilot-refinement catalog and bundle differ"
            )
        yield bundle


class DurableRecordingStarlinkPilotRefinementQueryV0_1:
    def __init__(
        self,
        store: DurableStarlinkPilotRefinementStoreV0_1,
        catalog: StarlinkPilotRefinementCatalogV0_1,
    ) -> None:
        self._store, self._catalog = store, catalog

    def recording_starlink_pilot_refinement(
        self, query: StarlinkPilotRefinementQueryV0_1
    ) -> RecordingStarlinkPilotRefinementViewV0_1:
        ref = self._catalog.latest_starlink_pilot_refinement(query.recording_id)
        if ref is None:
            raise LookupError("recording has no pilot-refinement evidence")
        with self._store.open(ref) as bundle:
            selected = tuple(
                stream
                for stream in bundle.streams
                if (not query.radio_ids or stream.selection.radio_id in query.radio_ids)
                and (not query.lnb_ids or stream.selection.lnb_id in query.lnb_ids)
                and (
                    not query.receiver_chain_ids
                    or stream.selection.receiver_chain_id in query.receiver_chain_ids
                )
                and (not query.edges or stream.selection.edge in query.edges)
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
                    StarlinkPilotRefinementPresentationStreamV0_1(
                        stream.selection,
                        shown,
                        len(points),
                        stream.exact_covered_sample_count,
                        stream.exact_coverage_fraction,
                    )
                )
            shown_count = sum(len(stream.points) for stream in views)
            return RecordingStarlinkPilotRefinementViewV0_1(
                SchemaRef(RecordingStarlinkPilotRefinementViewV0_1.SCHEMA_ID, V0_1),
                bundle.recording_id,
                ArtifactRef(bundle.analysis_id, ref.bundle_ref.digest, bundle.schema),
                bundle.source_prescreen_ref,
                bundle.source_suite_ref,
                tuple(views),
                original,
                shown_count < original,
                "none" if shown_count == original else "score-extrema-and-even-time",
                True,
                True,
                bundle.warnings,
            )


def starlink_pilot_refinement_projection_v0_1(
    request: StarlinkPilotRefinementRequestV0_1,
    bundle: StarlinkPilotRefinementBundleV0_1,
) -> StarlinkPilotRefinementCatalogProjectionV0_1:
    if (
        request.recording_id != bundle.recording_id
        or request.recording_object_ref.identity_digest()
        != bundle.recording_identity_digest
        or request.source_prescreen_ref != bundle.source_prescreen_ref
        or request.source_suite_ref != bundle.source_suite_ref
        or request.digest != bundle.request_digest
        or request.search_grid != bundle.search_grid
        or {stream.identity for stream in request.streams}
        != {stream.selection.identity for stream in bundle.streams}
    ):
        raise StarlinkPilotRefinementIntegrityError(
            "pilot-refinement request and bundle differ"
        )
    return _projection(bundle)


def _projection(
    bundle: StarlinkPilotRefinementBundleV0_1,
) -> StarlinkPilotRefinementCatalogProjectionV0_1:
    seed_count = sum(len(stream.selection.seeds) for stream in bundle.streams)
    return StarlinkPilotRefinementCatalogProjectionV0_1(
        bundle.analysis_id,
        bundle.recording_id,
        bundle.recording_identity_digest,
        bundle.source_prescreen_ref,
        bundle.source_suite_ref,
        bundle.request_digest,
        len(bundle.streams),
        seed_count,
        sum(len(stream.points) for stream in bundle.streams),
    )


def _bounded_points(
    points: tuple[StarlinkAdaptiveResponsePointV0_1, ...], maximum: int
) -> tuple[StarlinkAdaptiveResponsePointV0_1, ...]:
    if len(points) <= maximum:
        return points
    required = {
        points.index(max(points, key=lambda point: point.qin.score)),
        points.index(min(points, key=lambda point: point.qin.score)),
        points.index(max(points, key=lambda point: abs(point.qin_minus_max_surrogate))),
    }
    slots = max(0, maximum - len(required))
    required.update(
        round(index * (len(points) - 1) / max(1, slots - 1)) for index in range(slots)
    )
    return tuple(points[index] for index in sorted(required)[:maximum])
