"""CAS-first persistence and bounded presentation for adaptive QAM v0.4."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import ArtifactRef, Digest, RecordingId, SchemaRef
from leo_flow.contracts.starlink_acquired_constellation_pipeline import (
    StarlinkAcquiredConstellationQueryV0_3,
    StarlinkAcquiredConstellationViewMode,
    acquired_constellation_presentation_window,
)
from leo_flow.contracts.starlink_adaptive_qam import (
    V0_4,
    RecordingStarlinkAdaptiveQamViewV0_4,
    StarlinkAdaptiveQamBundleV0_4,
    StarlinkAdaptiveQamCatalogProjectionV0_4,
    StarlinkAdaptiveQamPresentationStreamV0_4,
    StarlinkAdaptiveQamPresentationWindowV0_4,
    StarlinkAdaptiveQamProductRefV0_4,
    StarlinkAdaptiveQamRequestV0_4,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .starlink_adaptive_qam_codec import (
    MAX_STARLINK_ADAPTIVE_QAM_BYTES,
    STARLINK_ADAPTIVE_QAM_FORMAT_ID,
    STARLINK_ADAPTIVE_QAM_MEDIA_TYPE,
    decode_starlink_adaptive_qam,
    encode_starlink_adaptive_qam,
)


class StarlinkAdaptiveQamNotFoundError(LookupError):
    pass


class StarlinkAdaptiveQamIntegrityError(RuntimeError):
    pass


class StarlinkAdaptiveQamConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogedStarlinkAdaptiveQamV0_4:
    projection: StarlinkAdaptiveQamCatalogProjectionV0_4
    bundle_ref: ObjectRef

    @property
    def ref(self) -> StarlinkAdaptiveQamProductRefV0_4:
        return StarlinkAdaptiveQamProductRefV0_4(
            self.projection.analysis_id,
            self.projection.recording_id,
            self.bundle_ref,
        )


class StarlinkAdaptiveQamCatalogV0_4(Protocol):
    def publish_starlink_adaptive_qam(
        self,
        projection: StarlinkAdaptiveQamCatalogProjectionV0_4,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkAdaptiveQamProductRefV0_4: ...

    def get_starlink_adaptive_qam(
        self, ref: StarlinkAdaptiveQamProductRefV0_4
    ) -> CatalogedStarlinkAdaptiveQamV0_4 | None: ...

    def latest_starlink_adaptive_qam(
        self, recording_id: RecordingId
    ) -> StarlinkAdaptiveQamProductRefV0_4 | None: ...


class StarlinkAdaptiveQamBlobStore(BlobReader, BlobWriter, Protocol):
    pass


class DurableStarlinkAdaptiveQamStoreV0_4:
    def __init__(
        self,
        blobs: StarlinkAdaptiveQamBlobStore,
        catalog: StarlinkAdaptiveQamCatalogV0_4,
    ) -> None:
        self._blobs, self._catalog = blobs, catalog

    def publish(
        self,
        request: StarlinkAdaptiveQamRequestV0_4,
        bundle: StarlinkAdaptiveQamBundleV0_4,
        *,
        idempotency_key: str,
    ) -> StarlinkAdaptiveQamProductRefV0_4:
        projection = starlink_adaptive_qam_projection_v0_4(request, bundle)
        payload = encode_starlink_adaptive_qam(bundle)
        blob = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=STARLINK_ADAPTIVE_QAM_MEDIA_TYPE,
            format_id=STARLINK_ADAPTIVE_QAM_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:bundle-v0.4",
        )
        expected = StarlinkAdaptiveQamProductRefV0_4(
            bundle.analysis_id, bundle.recording_id, blob
        )
        atomic = getattr(
            self._catalog, "publish_starlink_adaptive_qam_with_summary", None
        )
        if atomic is None:
            actual = self._catalog.publish_starlink_adaptive_qam(
                projection,
                blob,
                request.recording_object_ref,
                idempotency_key=idempotency_key,
            )
        else:
            actual = atomic(
                projection,
                blob,
                request.recording_object_ref,
                bundle,
                idempotency_key=idempotency_key,
            )
        if actual != expected:
            raise StarlinkAdaptiveQamConflictError(
                "adaptive QAM catalog replay returned another product"
            )
        return actual

    def open(
        self, ref: StarlinkAdaptiveQamProductRefV0_4
    ) -> AbstractContextManager[StarlinkAdaptiveQamBundleV0_4]:
        return self._open(ref)

    @contextmanager
    def _open(
        self, ref: StarlinkAdaptiveQamProductRefV0_4
    ) -> Iterator[StarlinkAdaptiveQamBundleV0_4]:
        cataloged = self._catalog.get_starlink_adaptive_qam(ref)
        if cataloged is None or cataloged.ref != ref:
            raise StarlinkAdaptiveQamNotFoundError("adaptive QAM was not found")
        blob = cataloged.bundle_ref
        if (
            blob.media_type != STARLINK_ADAPTIVE_QAM_MEDIA_TYPE
            or blob.format_id != STARLINK_ADAPTIVE_QAM_FORMAT_ID
            or blob.byte_count > MAX_STARLINK_ADAPTIVE_QAM_BYTES
        ):
            raise StarlinkAdaptiveQamIntegrityError("adaptive QAM metadata is invalid")
        metadata = self._blobs.head(blob)
        if metadata.ref != blob or not metadata.verified:
            raise StarlinkAdaptiveQamIntegrityError("adaptive QAM blob is unverified")
        with self._blobs.open(blob) as stream:
            payload = stream.read(MAX_STARLINK_ADAPTIVE_QAM_BYTES + 1)
        if len(payload) != blob.byte_count or Digest.sha256(payload) != blob.digest:
            raise StarlinkAdaptiveQamIntegrityError("adaptive QAM bytes differ")
        try:
            bundle = decode_starlink_adaptive_qam(payload)
        except ValueError as error:
            raise StarlinkAdaptiveQamIntegrityError(
                "adaptive QAM bundle is malformed"
            ) from error
        if (
            bundle.analysis_id != ref.analysis_id
            or bundle.recording_id != ref.recording_id
            or _projection(bundle) != cataloged.projection
        ):
            raise StarlinkAdaptiveQamIntegrityError(
                "adaptive QAM catalog and bundle differ"
            )
        yield bundle


class DurableRecordingStarlinkAdaptiveQamQueryV0_4:
    def __init__(
        self,
        store: DurableStarlinkAdaptiveQamStoreV0_4,
        catalog: StarlinkAdaptiveQamCatalogV0_4,
    ) -> None:
        self._store, self._catalog = store, catalog

    def recording_starlink_adaptive_qam(
        self, query: StarlinkAcquiredConstellationQueryV0_3
    ) -> RecordingStarlinkAdaptiveQamViewV0_4:
        ref = self._catalog.latest_starlink_adaptive_qam(query.recording_id)
        if ref is None:
            raise StarlinkAdaptiveQamNotFoundError(
                "recording has no adaptive QAM product"
            )
        with self._store.open(ref) as bundle:
            streams = []
            truncated = False
            for selection, evidence in zip(
                bundle.stream_selections, bundle.evidence_bundle.streams, strict=True
            ):
                if (
                    (query.radio_ids and selection.radio_id not in query.radio_ids)
                    or (query.lnb_ids and selection.lnb_id not in query.lnb_ids)
                    or (
                        query.segment_ids
                        and selection.segment_id not in query.segment_ids
                    )
                    or (
                        query.receiver_chain_ids
                        and selection.receiver_chain_id not in query.receiver_chain_ids
                    )
                    or (query.edges and selection.edge not in query.edges)
                ):
                    continue
                pairs = tuple(zip(selection.windows, evidence.windows, strict=True))
                if query.mode is StarlinkAcquiredConstellationViewMode.OVERALL:
                    pairs = (pairs[evidence.overall.selected_display_window_index],)
                elif len(pairs) > query.maximum_windows_per_stream:
                    pairs = pairs[: query.maximum_windows_per_stream]
                    truncated = True
                windows = tuple(
                    StarlinkAdaptiveQamPresentationWindowV0_4(
                        selected,
                        acquired_constellation_presentation_window(
                            window, query.maximum_points_per_constellation
                        ),
                    )
                    for selected, window in pairs
                )
                streams.append(
                    StarlinkAdaptiveQamPresentationStreamV0_4(
                        selection.radio_id,
                        selection.lnb_id,
                        selection.segment_id,
                        selection.receiver_chain_id,
                        selection.channel_number,
                        selection.edge,
                        selection.sample_rate_hz,
                        selection.segment_sample_count,
                        evidence.overall,
                        windows,
                        len(evidence.windows),
                    )
                )
                if len(streams) >= query.maximum_streams:
                    truncated |= len(bundle.stream_selections) > len(streams)
                    break
            return RecordingStarlinkAdaptiveQamViewV0_4(
                SchemaRef(RecordingStarlinkAdaptiveQamViewV0_4.SCHEMA_ID, V0_4),
                bundle.recording_id,
                ArtifactRef(bundle.analysis_id, ref.bundle_ref.digest, bundle.schema),
                bundle.source_adaptive_response_ref,
                query.mode,
                tuple(streams),
                truncated,
                True,
                True,
                bundle.warnings,
            )


def starlink_adaptive_qam_projection_v0_4(
    request: StarlinkAdaptiveQamRequestV0_4,
    bundle: StarlinkAdaptiveQamBundleV0_4,
) -> StarlinkAdaptiveQamCatalogProjectionV0_4:
    if (
        request.recording_id != bundle.recording_id
        or request.recording_object_ref.identity_digest()
        != bundle.recording_identity_digest
        or request.source_adaptive_response_ref != bundle.source_adaptive_response_ref
        or request.source_suite_ref != bundle.source_suite_ref
        or request.streams != bundle.stream_selections
        or request.digest != bundle.request_digest
        or request.requested_output_schema != bundle.schema
    ):
        raise StarlinkAdaptiveQamIntegrityError(
            "adaptive QAM request and bundle differ"
        )
    return _projection(bundle)


def _projection(
    bundle: StarlinkAdaptiveQamBundleV0_4,
) -> StarlinkAdaptiveQamCatalogProjectionV0_4:
    windows = sum(len(item.windows) for item in bundle.stream_selections)
    return StarlinkAdaptiveQamCatalogProjectionV0_4(
        bundle.analysis_id,
        bundle.recording_id,
        bundle.recording_identity_digest,
        bundle.source_adaptive_response_ref,
        bundle.source_suite_ref,
        bundle.request_digest,
        len(bundle.stream_selections),
        windows,
        windows * 2400,
    )
