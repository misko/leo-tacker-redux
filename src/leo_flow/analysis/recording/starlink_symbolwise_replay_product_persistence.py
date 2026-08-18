"""CAS-first persistence and bounded query for recording symbolwise replay."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import ArtifactRef, Digest, RecordingId, SchemaRef
from leo_flow.contracts.starlink_symbolwise_replay import (
    StarlinkSymbolwiseWindowEvidenceV0_1,
)
from leo_flow.contracts.starlink_symbolwise_replay_product import (
    V0_1,
    RecordingStarlinkSymbolwiseReplayViewV0_1,
    StarlinkSymbolwiseRecordingBundleV0_1,
    StarlinkSymbolwiseRecordingProductRefV0_1,
    StarlinkSymbolwiseReplayCatalogProjectionV0_1,
    StarlinkSymbolwiseReplayPresentationStreamV0_1,
    StarlinkSymbolwiseReplayPublicationFenceV0_1,
    StarlinkSymbolwiseReplayQueryV0_1,
    StarlinkSymbolwiseReplayRequestV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .starlink_symbolwise_replay import (
    StarlinkSymbolwiseReplayConfigV0_1,
    starlink_symbolwise_replay_algorithm_ref_v0_1,
    starlink_symbolwise_replay_config_ref_v0_1,
)
from .starlink_symbolwise_replay_product_codec import (
    MAX_STARLINK_SYMBOLWISE_RECORDING_BUNDLE_BYTES,
    STARLINK_SYMBOLWISE_RECORDING_FORMAT_ID,
    STARLINK_SYMBOLWISE_RECORDING_MEDIA_TYPE,
    decode_starlink_symbolwise_recording_bundle,
    encode_starlink_symbolwise_recording_bundle,
)


class StarlinkSymbolwiseReplayIntegrityError(RuntimeError):
    pass


class StarlinkSymbolwiseReplayNotFoundError(LookupError):
    pass


class StarlinkSymbolwiseReplayConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogedStarlinkSymbolwiseReplayV0_1:
    projection: StarlinkSymbolwiseReplayCatalogProjectionV0_1
    bundle_ref: ObjectRef

    @property
    def ref(self) -> StarlinkSymbolwiseRecordingProductRefV0_1:
        return StarlinkSymbolwiseRecordingProductRefV0_1(
            self.projection.analysis_id,
            self.projection.recording_id,
            self.bundle_ref,
        )


class StarlinkSymbolwiseReplayCatalogV0_1(Protocol):
    def publish_starlink_symbolwise_replay(
        self,
        projection: StarlinkSymbolwiseReplayCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        lease_fence: StarlinkSymbolwiseReplayPublicationFenceV0_1,
        idempotency_key: str,
    ) -> StarlinkSymbolwiseRecordingProductRefV0_1: ...

    def get_starlink_symbolwise_replay(
        self, ref: StarlinkSymbolwiseRecordingProductRefV0_1
    ) -> CatalogedStarlinkSymbolwiseReplayV0_1 | None: ...

    def latest_starlink_symbolwise_replay(
        self, recording_id: RecordingId
    ) -> StarlinkSymbolwiseRecordingProductRefV0_1 | None: ...


class StarlinkSymbolwiseReplayBlobStore(BlobReader, BlobWriter, Protocol):
    pass


class DurableStarlinkSymbolwiseReplayStoreV0_1:
    def __init__(
        self,
        blobs: StarlinkSymbolwiseReplayBlobStore,
        catalog: StarlinkSymbolwiseReplayCatalogV0_1,
    ) -> None:
        self._blobs, self._catalog = blobs, catalog

    def publish(
        self,
        request: StarlinkSymbolwiseReplayRequestV0_1,
        bundle: StarlinkSymbolwiseRecordingBundleV0_1,
        *,
        lease_fence: StarlinkSymbolwiseReplayPublicationFenceV0_1,
        idempotency_key: str,
    ) -> StarlinkSymbolwiseRecordingProductRefV0_1:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        projection = starlink_symbolwise_replay_projection_v0_1(request, bundle)
        payload = encode_starlink_symbolwise_recording_bundle(bundle)
        blob = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=STARLINK_SYMBOLWISE_RECORDING_MEDIA_TYPE,
            format_id=STARLINK_SYMBOLWISE_RECORDING_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:bundle-v0.1",
        )
        expected = StarlinkSymbolwiseRecordingProductRefV0_1(
            bundle.analysis_id, bundle.recording_id, blob
        )
        actual = self._catalog.publish_starlink_symbolwise_replay(
            projection,
            blob,
            request.recording_object_ref,
            lease_fence=lease_fence,
            idempotency_key=idempotency_key,
        )
        if actual != expected:
            raise StarlinkSymbolwiseReplayConflictError(
                "catalog replay returned another symbolwise product"
            )
        return actual

    def open(
        self, ref: StarlinkSymbolwiseRecordingProductRefV0_1
    ) -> AbstractContextManager[StarlinkSymbolwiseRecordingBundleV0_1]:
        return self._open(ref)

    @contextmanager
    def _open(
        self, ref: StarlinkSymbolwiseRecordingProductRefV0_1
    ) -> Iterator[StarlinkSymbolwiseRecordingBundleV0_1]:
        cataloged = self._catalog.get_starlink_symbolwise_replay(ref)
        if cataloged is None or cataloged.ref != ref:
            raise StarlinkSymbolwiseReplayNotFoundError(
                "recording symbolwise replay was not found"
            )
        blob = cataloged.bundle_ref
        if (
            blob.media_type != STARLINK_SYMBOLWISE_RECORDING_MEDIA_TYPE
            or blob.format_id != STARLINK_SYMBOLWISE_RECORDING_FORMAT_ID
            or blob.byte_count > MAX_STARLINK_SYMBOLWISE_RECORDING_BUNDLE_BYTES
        ):
            raise StarlinkSymbolwiseReplayIntegrityError(
                "recording symbolwise replay metadata is invalid"
            )
        metadata = self._blobs.head(blob)
        if metadata.ref != blob or not metadata.verified:
            raise StarlinkSymbolwiseReplayIntegrityError(
                "recording symbolwise replay blob is not verified"
            )
        with self._blobs.open(blob) as stream:
            payload = stream.read(MAX_STARLINK_SYMBOLWISE_RECORDING_BUNDLE_BYTES + 1)
        if len(payload) != blob.byte_count or Digest.sha256(payload) != blob.digest:
            raise StarlinkSymbolwiseReplayIntegrityError(
                "recording symbolwise replay bytes differ"
            )
        try:
            bundle = decode_starlink_symbolwise_recording_bundle(payload)
        except ValueError as error:
            raise StarlinkSymbolwiseReplayIntegrityError(
                "recording symbolwise replay bundle is invalid"
            ) from error
        if (
            bundle.analysis_id != ref.analysis_id
            or bundle.recording_id != ref.recording_id
            or _projection(bundle) != cataloged.projection
        ):
            raise StarlinkSymbolwiseReplayIntegrityError(
                "recording symbolwise catalog and bundle differ"
            )
        yield bundle


class DurableRecordingStarlinkSymbolwiseReplayQueryV0_1:
    def __init__(
        self,
        store: DurableStarlinkSymbolwiseReplayStoreV0_1,
        catalog: StarlinkSymbolwiseReplayCatalogV0_1,
    ) -> None:
        self._store, self._catalog = store, catalog

    def recording_starlink_symbolwise_replay(
        self, query: StarlinkSymbolwiseReplayQueryV0_1
    ) -> RecordingStarlinkSymbolwiseReplayViewV0_1:
        ref = self._catalog.latest_starlink_symbolwise_replay(query.recording_id)
        if ref is None:
            raise StarlinkSymbolwiseReplayNotFoundError(
                "recording has no symbolwise replay product"
            )
        with self._store.open(ref) as bundle:
            selected = tuple(
                (selection, stream)
                for selection, stream in zip(
                    bundle.stream_selections, bundle.streams, strict=True
                )
                if not query.receiver_chain_ids
                or selection.receiver_chain_id in query.receiver_chain_ids
            )
            ranged = tuple(
                (
                    selection,
                    stream,
                    stream.windows[query.first_window_index : query.stop_window_index],
                )
                for selection, stream in selected
            )
            original = sum(len(windows) for _selection, _stream, windows in ranged)
            remaining = query.maximum_windows
            views = []
            for index, (selection, stream, windows) in enumerate(ranged):
                stream_count = len(ranged) - index
                budget = (
                    (remaining + stream_count - 1) // stream_count
                    if remaining > 0
                    else 0
                )
                shown = _bounded_windows(windows, budget)
                remaining -= len(shown)
                views.append(
                    StarlinkSymbolwiseReplayPresentationStreamV0_1(
                        selection, len(stream.windows), shown
                    )
                )
        shown_count = sum(len(stream.windows) for stream in views)
        return RecordingStarlinkSymbolwiseReplayViewV0_1(
            SchemaRef(RecordingStarlinkSymbolwiseReplayViewV0_1.SCHEMA_ID, V0_1),
            bundle.recording_id,
            ArtifactRef(bundle.analysis_id, ref.bundle_ref.digest, bundle.schema),
            tuple(views),
            original,
            shown_count,
            shown_count < original,
            "even-index-preserving" if shown_count < original else "complete",
            True,
            bundle.reason_codes,
        )


def starlink_symbolwise_replay_projection_v0_1(
    request: StarlinkSymbolwiseReplayRequestV0_1,
    bundle: StarlinkSymbolwiseRecordingBundleV0_1,
) -> StarlinkSymbolwiseReplayCatalogProjectionV0_1:
    expected_config = StarlinkSymbolwiseReplayConfigV0_1(
        surrogate_count=request.plan.surrogate_count,
        maximum_windows=request.plan.maximum_windows,
        maximum_window_samples=request.plan.maximum_window_samples,
        maximum_timing_search_cells=request.plan.maximum_timing_search_cells,
        maximum_refinement_search_cells=request.plan.maximum_refinement_search_cells,
        maximum_working_bytes=request.plan.maximum_working_bytes,
    )
    expected_config_ref = starlink_symbolwise_replay_config_ref_v0_1(expected_config)
    expected_algorithm_ref = starlink_symbolwise_replay_algorithm_ref_v0_1()
    if (
        request.recording_id != bundle.recording_id
        or request.recording_object_ref.identity_digest()
        != bundle.recording_identity_digest
        or request.digest != bundle.request_digest
        or request.plan != bundle.plan
        or request.stream_selections != bundle.stream_selections
        or request.requested_output_schema != bundle.schema
        or any(
            stream.config_ref != expected_config_ref
            or stream.algorithm_ref != expected_algorithm_ref
            for stream in bundle.streams
        )
    ):
        raise StarlinkSymbolwiseReplayIntegrityError(
            "symbolwise replay request and bundle differ"
        )
    return _projection(bundle)


def _projection(
    bundle: StarlinkSymbolwiseRecordingBundleV0_1,
) -> StarlinkSymbolwiseReplayCatalogProjectionV0_1:
    return StarlinkSymbolwiseReplayCatalogProjectionV0_1(
        bundle.analysis_id,
        bundle.recording_id,
        bundle.recording_identity_digest,
        bundle.request_digest,
        len(bundle.streams),
        bundle.total_window_count,
        bundle.total_pattern_evidence_count,
        bundle.candidates_only,
    )


def _bounded_windows(
    windows: tuple[StarlinkSymbolwiseWindowEvidenceV0_1, ...], maximum: int
) -> tuple[StarlinkSymbolwiseWindowEvidenceV0_1, ...]:
    if maximum <= 0:
        return ()
    if len(windows) <= maximum:
        return windows
    if maximum == 1:
        return (windows[0],)
    indices = tuple(
        round(index * (len(windows) - 1) / (maximum - 1)) for index in range(maximum)
    )
    return tuple(windows[index] for index in indices)
