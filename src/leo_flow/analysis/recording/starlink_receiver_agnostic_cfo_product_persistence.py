"""CAS-first durable store and bounded query for CFO/QAM v0.6 products."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import ArtifactRef, Digest, RecordingId, SchemaRef
from leo_flow.contracts.starlink_receiver_agnostic_cfo import V0_6
from leo_flow.contracts.starlink_receiver_agnostic_cfo_product import (
    ReceiverAgnosticCfoQamCatalogProjectionV0_6,
    ReceiverAgnosticCfoQamPatternSummaryV0_6,
    ReceiverAgnosticCfoQamQueryV0_6,
    ReceiverAgnosticCfoQamRecordingBundleV0_6,
    ReceiverAgnosticCfoQamRecordingProductRefV0_6,
    ReceiverAgnosticCfoQamRecordingRequestV0_6,
    ReceiverAgnosticCfoQamWindowSummaryV0_6,
    RecordingReceiverAgnosticCfoQamViewV0_6,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .starlink_receiver_agnostic_cfo_product_codec import (
    MAX_RECEIVER_AGNOSTIC_CFO_QAM_BUNDLE_BYTES,
    RECEIVER_AGNOSTIC_CFO_QAM_FORMAT_ID,
    RECEIVER_AGNOSTIC_CFO_QAM_MEDIA_TYPE,
    decode_receiver_agnostic_cfo_qam_bundle,
    encode_receiver_agnostic_cfo_qam_bundle,
)


class ReceiverAgnosticCfoQamNotFoundError(LookupError):
    pass


class ReceiverAgnosticCfoQamIntegrityError(RuntimeError):
    pass


class ReceiverAgnosticCfoQamConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogedReceiverAgnosticCfoQamV0_6:
    projection: ReceiverAgnosticCfoQamCatalogProjectionV0_6
    bundle_ref: ObjectRef

    @property
    def ref(self) -> ReceiverAgnosticCfoQamRecordingProductRefV0_6:
        return ReceiverAgnosticCfoQamRecordingProductRefV0_6(
            self.projection.analysis_id,
            self.projection.recording_id,
            self.bundle_ref,
        )


class ReceiverAgnosticCfoQamCatalogV0_6(Protocol):
    def publish_receiver_agnostic_cfo_qam(
        self,
        projection: ReceiverAgnosticCfoQamCatalogProjectionV0_6,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> ReceiverAgnosticCfoQamRecordingProductRefV0_6: ...

    def get_receiver_agnostic_cfo_qam(
        self, ref: ReceiverAgnosticCfoQamRecordingProductRefV0_6
    ) -> CatalogedReceiverAgnosticCfoQamV0_6 | None: ...

    def latest_receiver_agnostic_cfo_qam(
        self, recording_id: RecordingId
    ) -> ReceiverAgnosticCfoQamRecordingProductRefV0_6 | None: ...


class ReceiverAgnosticCfoQamBlobStore(BlobReader, BlobWriter, Protocol):
    pass


class DurableReceiverAgnosticCfoQamStoreV0_6:
    def __init__(
        self,
        blobs: ReceiverAgnosticCfoQamBlobStore,
        catalog: ReceiverAgnosticCfoQamCatalogV0_6,
    ) -> None:
        self._blobs, self._catalog = blobs, catalog

    def publish(
        self,
        request: ReceiverAgnosticCfoQamRecordingRequestV0_6,
        bundle: ReceiverAgnosticCfoQamRecordingBundleV0_6,
        *,
        idempotency_key: str,
    ) -> ReceiverAgnosticCfoQamRecordingProductRefV0_6:
        projection = receiver_agnostic_cfo_qam_projection_v0_6(request, bundle)
        payload = encode_receiver_agnostic_cfo_qam_bundle(bundle)
        blob = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=RECEIVER_AGNOSTIC_CFO_QAM_MEDIA_TYPE,
            format_id=RECEIVER_AGNOSTIC_CFO_QAM_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:bundle-v0.6",
        )
        expected = ReceiverAgnosticCfoQamRecordingProductRefV0_6(
            bundle.analysis_id, bundle.recording_id, blob
        )
        actual = self._catalog.publish_receiver_agnostic_cfo_qam(
            projection,
            blob,
            request.recording_object_ref,
            idempotency_key=idempotency_key,
        )
        if actual != expected:
            raise ReceiverAgnosticCfoQamConflictError(
                "receiver-agnostic CFO/QAM catalog returned another product"
            )
        return actual

    def open(
        self, ref: ReceiverAgnosticCfoQamRecordingProductRefV0_6
    ) -> AbstractContextManager[ReceiverAgnosticCfoQamRecordingBundleV0_6]:
        return self._open(ref)

    @contextmanager
    def _open(
        self, ref: ReceiverAgnosticCfoQamRecordingProductRefV0_6
    ) -> Iterator[ReceiverAgnosticCfoQamRecordingBundleV0_6]:
        cataloged = self._catalog.get_receiver_agnostic_cfo_qam(ref)
        if cataloged is None or cataloged.ref != ref:
            raise ReceiverAgnosticCfoQamNotFoundError(
                "receiver-agnostic CFO/QAM product was not found"
            )
        blob = cataloged.bundle_ref
        if (
            blob.media_type != RECEIVER_AGNOSTIC_CFO_QAM_MEDIA_TYPE
            or blob.format_id != RECEIVER_AGNOSTIC_CFO_QAM_FORMAT_ID
            or blob.byte_count > MAX_RECEIVER_AGNOSTIC_CFO_QAM_BUNDLE_BYTES
        ):
            raise ReceiverAgnosticCfoQamIntegrityError(
                "receiver-agnostic CFO/QAM blob metadata is invalid"
            )
        metadata = self._blobs.head(blob)
        if metadata.ref != blob or not metadata.verified:
            raise ReceiverAgnosticCfoQamIntegrityError(
                "receiver-agnostic CFO/QAM blob is unverified"
            )
        with self._blobs.open(blob) as stream:
            payload = stream.read(MAX_RECEIVER_AGNOSTIC_CFO_QAM_BUNDLE_BYTES + 1)
        if len(payload) != blob.byte_count or Digest.sha256(payload) != blob.digest:
            raise ReceiverAgnosticCfoQamIntegrityError(
                "receiver-agnostic CFO/QAM bytes differ"
            )
        try:
            bundle = decode_receiver_agnostic_cfo_qam_bundle(payload)
        except ValueError as error:
            raise ReceiverAgnosticCfoQamIntegrityError(
                "receiver-agnostic CFO/QAM bundle is malformed"
            ) from error
        if (
            bundle.analysis_id != ref.analysis_id
            or bundle.recording_id != ref.recording_id
            or _projection(bundle) != cataloged.projection
        ):
            raise ReceiverAgnosticCfoQamIntegrityError(
                "receiver-agnostic CFO/QAM catalog and bundle differ"
            )
        yield bundle


class DurableRecordingReceiverAgnosticCfoQamQueryV0_6:
    def __init__(
        self,
        store: DurableReceiverAgnosticCfoQamStoreV0_6,
        catalog: ReceiverAgnosticCfoQamCatalogV0_6,
    ) -> None:
        self._store, self._catalog = store, catalog

    def recording_receiver_agnostic_cfo_qam(
        self, query: ReceiverAgnosticCfoQamQueryV0_6
    ) -> RecordingReceiverAgnosticCfoQamViewV0_6:
        ref = self._catalog.latest_receiver_agnostic_cfo_qam(query.recording_id)
        if ref is None:
            raise ReceiverAgnosticCfoQamNotFoundError(
                "recording has no receiver-agnostic CFO/QAM product"
            )
        with self._store.open(ref) as bundle:
            eligible = tuple(
                item
                for item in bundle.window_products
                if (not query.radio_ids or item.window.radio_id in query.radio_ids)
                and (
                    not query.receiver_chain_ids
                    or item.window.receiver_chain_id in query.receiver_chain_ids
                )
            )
            shown = eligible[: query.maximum_windows]
            summaries = tuple(_window_summary(item) for item in shown)
            return RecordingReceiverAgnosticCfoQamViewV0_6(
                SchemaRef(RecordingReceiverAgnosticCfoQamViewV0_6.SCHEMA_ID, V0_6),
                bundle.recording_id,
                ArtifactRef(bundle.analysis_id, ref.bundle_ref.digest, bundle.schema),
                summaries,
                len(eligible),
                len(summaries),
                len(summaries) < len(eligible),
                True,
                None,
                tuple(sorted(set(bundle.disclosures))),
            )


def receiver_agnostic_cfo_qam_projection_v0_6(
    request: ReceiverAgnosticCfoQamRecordingRequestV0_6,
    bundle: ReceiverAgnosticCfoQamRecordingBundleV0_6,
) -> ReceiverAgnosticCfoQamCatalogProjectionV0_6:
    if (
        request.recording_id != bundle.recording_id
        or request.recording_object_ref.identity_digest()
        != bundle.recording_identity_digest
        or request.digest != bundle.request_digest
        or request.plan != bundle.plan
        or request.windows != tuple(item.window for item in bundle.window_products)
        or request.requested_output_schema != bundle.schema
    ):
        raise ReceiverAgnosticCfoQamIntegrityError(
            "receiver-agnostic CFO/QAM request and bundle differ"
        )
    return _projection(bundle)


def _projection(
    bundle: ReceiverAgnosticCfoQamRecordingBundleV0_6,
) -> ReceiverAgnosticCfoQamCatalogProjectionV0_6:
    return ReceiverAgnosticCfoQamCatalogProjectionV0_6(
        bundle.analysis_id,
        bundle.recording_id,
        bundle.recording_identity_digest,
        bundle.request_digest,
        bundle.stream_count,
        bundle.window_count,
        bundle.pattern_evidence_count,
        bundle.unique_cell_count,
        bundle.pattern_evaluation_count,
        bundle.candidates_only,
    )


def _window_summary(item) -> ReceiverAgnosticCfoQamWindowSummaryV0_6:
    receipt = item.search_receipt
    return ReceiverAgnosticCfoQamWindowSummaryV0_6(
        item.window.radio_id,
        item.window.receiver_chain_id,
        item.window.edge,
        item.window.start_sample,
        item.window.stop_sample,
        item.window.sample_rate_hz,
        receipt.plan.cfo_min_hz,
        receipt.plan.cfo_max_hz,
        receipt.coarse_cell_count,
        receipt.local_cell_count,
        receipt.unique_cell_count,
        receipt.pattern_evaluation_count,
        tuple(
            ReceiverAgnosticCfoQamPatternSummaryV0_6(
                evidence.pattern_index,
                evidence.role,
                evidence.template_ref,
                evidence.winner.epoch_sample,
                evidence.winner.cfo_hz,
                evidence.winner.score,
                evidence.complete_frame_count,
                evidence.hard_symbol_accuracy,
                evidence.rms_evm,
                evidence.qam_goodness,
            )
            for evidence in item.pattern_qam
        ),
    )
