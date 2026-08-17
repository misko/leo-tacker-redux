"""Integrity closure, catalog ports and bounded reads for surrogate-null evidence."""

from __future__ import annotations

import io
import math
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import Digest, RecordingId, SchemaRef
from leo_flow.contracts.starlink_detector_suite import (
    REPORT_METHOD_ORDER,
    StarlinkDetectorMethod,
)
from leo_flow.contracts.starlink_surrogate_null import V0_1
from leo_flow.contracts.starlink_surrogate_null_pipeline import (
    RecordingStarlinkSurrogateNullViewV0_1,
    StarlinkSurrogateNullCatalogProjectionV0_1,
    StarlinkSurrogateNullMethodAggregateV0_1,
    StarlinkSurrogateNullMethodRowV0_1,
    StarlinkSurrogateNullProductRefV0_1,
    StarlinkSurrogateNullQueryV0_1,
    StarlinkSurrogateNullRecordingBundleV0_1,
    StarlinkSurrogateNullRequestV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .starlink_surrogate_null_recording_codec import (
    MAX_STARLINK_SURROGATE_NULL_RECORDING_BYTES,
    STARLINK_SURROGATE_NULL_RECORDING_FORMAT_ID,
    STARLINK_SURROGATE_NULL_RECORDING_MEDIA_TYPE,
    decode_starlink_surrogate_null_recording,
    encode_starlink_surrogate_null_recording,
)


class StarlinkSurrogateNullIntegrityError(RuntimeError):
    pass


class StarlinkSurrogateNullNotFoundError(LookupError):
    pass


class StarlinkSurrogateNullConflictError(RuntimeError):
    """Raised when an idempotency key or identity is reused for other bytes."""


@dataclass(frozen=True)
class CatalogedStarlinkSurrogateNullV0_1:
    projection: StarlinkSurrogateNullCatalogProjectionV0_1
    bundle_ref: ObjectRef

    @property
    def ref(self) -> StarlinkSurrogateNullProductRefV0_1:
        return StarlinkSurrogateNullProductRefV0_1(
            self.projection.analysis_id,
            self.projection.recording_id,
            self.bundle_ref,
        )


class StarlinkSurrogateNullCatalogV0_1(Protocol):
    """Exact-replay catalog port; conflicting reuse must raise ConflictError."""

    def publish_starlink_surrogate_null(
        self,
        projection: StarlinkSurrogateNullCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkSurrogateNullProductRefV0_1: ...

    def get_starlink_surrogate_null(
        self, ref: StarlinkSurrogateNullProductRefV0_1
    ) -> CatalogedStarlinkSurrogateNullV0_1 | None: ...

    def latest_starlink_surrogate_null(
        self, recording_id: RecordingId
    ) -> StarlinkSurrogateNullProductRefV0_1 | None: ...


class StarlinkSurrogateNullBlobStore(BlobReader, BlobWriter, Protocol):
    pass


@dataclass(frozen=True)
class DurableStarlinkSurrogateNullViewV0_1:
    ref: StarlinkSurrogateNullProductRefV0_1
    bundle: StarlinkSurrogateNullRecordingBundleV0_1


class DurableStarlinkSurrogateNullStoreV0_1:
    def __init__(
        self,
        blobs: StarlinkSurrogateNullBlobStore,
        catalog: StarlinkSurrogateNullCatalogV0_1,
    ) -> None:
        self._blobs = blobs
        self._catalog = catalog

    def publish(
        self,
        request: StarlinkSurrogateNullRequestV0_1,
        bundle: StarlinkSurrogateNullRecordingBundleV0_1,
        *,
        idempotency_key: str,
    ) -> StarlinkSurrogateNullProductRefV0_1:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        projection = starlink_surrogate_null_projection_v0_1(request, bundle)
        payload = encode_starlink_surrogate_null_recording(bundle)
        blob = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=STARLINK_SURROGATE_NULL_RECORDING_MEDIA_TYPE,
            format_id=STARLINK_SURROGATE_NULL_RECORDING_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:bundle-v0.1",
        )
        expected = StarlinkSurrogateNullProductRefV0_1(
            bundle.analysis_id,
            bundle.recording_id,
            blob,
        )
        actual = self._catalog.publish_starlink_surrogate_null(
            projection,
            blob,
            request.recording_object_ref,
            idempotency_key=idempotency_key,
        )
        if actual != expected:
            raise StarlinkSurrogateNullConflictError(
                "catalog replay returned another surrogate-null product"
            )
        return actual

    def open(
        self, ref: StarlinkSurrogateNullProductRefV0_1
    ) -> AbstractContextManager[DurableStarlinkSurrogateNullViewV0_1]:
        return self._open(ref)

    @contextmanager
    def _open(
        self, ref: StarlinkSurrogateNullProductRefV0_1
    ) -> Iterator[DurableStarlinkSurrogateNullViewV0_1]:
        cataloged = self._catalog.get_starlink_surrogate_null(ref)
        if cataloged is None:
            raise StarlinkSurrogateNullNotFoundError(
                "surrogate-null product was not found"
            )
        blob = cataloged.bundle_ref
        if (
            cataloged.ref != ref
            or blob.media_type != STARLINK_SURROGATE_NULL_RECORDING_MEDIA_TYPE
            or blob.format_id != STARLINK_SURROGATE_NULL_RECORDING_FORMAT_ID
            or blob.byte_count > MAX_STARLINK_SURROGATE_NULL_RECORDING_BYTES
        ):
            raise StarlinkSurrogateNullIntegrityError(
                "surrogate-null bundle metadata is invalid"
            )
        metadata = self._blobs.head(blob)
        if metadata.ref != blob or not metadata.verified:
            raise StarlinkSurrogateNullIntegrityError(
                "surrogate-null blob is not verified"
            )
        with self._blobs.open(blob) as stream:
            payload = stream.read(MAX_STARLINK_SURROGATE_NULL_RECORDING_BYTES + 1)
        if len(payload) != blob.byte_count or Digest.sha256(payload) != blob.digest:
            raise StarlinkSurrogateNullIntegrityError("surrogate-null bytes differ")
        try:
            bundle = decode_starlink_surrogate_null_recording(payload)
        except ValueError as error:
            raise StarlinkSurrogateNullIntegrityError(
                "surrogate-null bundle is invalid"
            ) from error
        if (
            bundle.analysis_id != ref.analysis_id
            or bundle.recording_id != ref.recording_id
            or _projection(bundle) != cataloged.projection
        ):
            raise StarlinkSurrogateNullIntegrityError(
                "surrogate-null catalog and bundle differ"
            )
        yield DurableStarlinkSurrogateNullViewV0_1(ref, bundle)


class DurableRecordingStarlinkSurrogateNullQueryV0_1:
    def __init__(
        self,
        store: DurableStarlinkSurrogateNullStoreV0_1,
        catalog: StarlinkSurrogateNullCatalogV0_1,
    ) -> None:
        self._store = store
        self._catalog = catalog

    def recording_starlink_surrogate_null(
        self, query: StarlinkSurrogateNullQueryV0_1
    ) -> RecordingStarlinkSurrogateNullViewV0_1:
        ref = self._catalog.latest_starlink_surrogate_null(query.recording_id)
        if ref is None:
            raise StarlinkSurrogateNullNotFoundError(
                "recording has no surrogate-null product"
            )
        with self._store.open(ref) as durable:
            return starlink_surrogate_null_view_v0_1(
                durable.bundle,
                durable.ref,
                query,
            )


def starlink_surrogate_null_projection_v0_1(
    request: StarlinkSurrogateNullRequestV0_1,
    bundle: StarlinkSurrogateNullRecordingBundleV0_1,
) -> StarlinkSurrogateNullCatalogProjectionV0_1:
    if (
        request.requested_output_schema != bundle.schema
        or request.recording_id != bundle.recording_id
        or request.recording_object_ref.identity_digest()
        != bundle.recording_identity_digest
        or request.source_suite_ref != bundle.source_suite_ref
        or request.source_suite_request_digest != bundle.source_suite_request_digest
        or request.digest != bundle.request_digest
    ):
        raise StarlinkSurrogateNullIntegrityError(
            "surrogate-null request and bundle differ"
        )
    expected = {
        (item.segment_id, item.receiver_chain_id) for item in request.stream_selections
    }
    actual = {(item.segment_id, item.receiver_chain_id) for item in bundle.streams}
    if expected != actual:
        raise StarlinkSurrogateNullIntegrityError(
            "surrogate-null stream membership differs"
        )
    if any(
        len(item.evidence.surrogates) != request.surrogate_count
        or item.evidence.exact.search_grid != request.search_grid
        for item in bundle.streams
    ):
        raise StarlinkSurrogateNullIntegrityError(
            "surrogate-null search configuration differs"
        )
    return _projection(bundle)


def _projection(
    bundle: StarlinkSurrogateNullRecordingBundleV0_1,
) -> StarlinkSurrogateNullCatalogProjectionV0_1:
    method_count = sum(len(item.evidence.method_nulls) for item in bundle.streams)
    surrogate_score_count = sum(
        len(method.surrogate_scores)
        for item in bundle.streams
        for method in item.evidence.method_nulls
    )
    return StarlinkSurrogateNullCatalogProjectionV0_1(
        bundle.analysis_id,
        bundle.recording_id,
        bundle.recording_identity_digest,
        bundle.source_suite_ref,
        bundle.source_suite_request_digest,
        bundle.request_digest,
        bundle.state,
        len(bundle.streams),
        method_count,
        surrogate_score_count,
    )


def starlink_surrogate_null_view_v0_1(
    bundle: StarlinkSurrogateNullRecordingBundleV0_1,
    ref: StarlinkSurrogateNullProductRefV0_1,
    query: StarlinkSurrogateNullQueryV0_1,
) -> RecordingStarlinkSurrogateNullViewV0_1:
    if (
        bundle.recording_id != query.recording_id
        or ref.recording_id != query.recording_id
    ):
        raise ValueError("surrogate-null query belongs to another recording")
    rows: list[StarlinkSurrogateNullMethodRowV0_1] = []
    for stream in bundle.streams:
        if query.radio_ids and stream.radio_id not in query.radio_ids:
            continue
        if query.channel_numbers and stream.channel_number not in query.channel_numbers:
            continue
        if query.edges and stream.edge not in query.edges:
            continue
        if (
            query.interval_start_utc_ns is not None
            and stream.interval_stop_utc_ns <= query.interval_start_utc_ns
        ):
            continue
        if (
            query.interval_stop_utc_ns is not None
            and stream.interval_start_utc_ns >= query.interval_stop_utc_ns
        ):
            continue
        exact = {item.method: item for item in stream.evidence.exact.methods}
        patterns = tuple(item.pattern for item in stream.evidence.surrogates)
        for null in stream.evidence.method_nulls:
            if null.method not in query.methods:
                continue
            method = exact[null.method]
            rows.append(
                StarlinkSurrogateNullMethodRowV0_1(
                    stream.radio_id,
                    stream.segment_id,
                    stream.receiver_chain_id,
                    stream.channel_number,
                    stream.edge,
                    stream.interval_start_utc_ns,
                    stream.interval_stop_utc_ns,
                    null.method,
                    null.target_score,
                    null.surrogate_scores,
                    null.empirical_upper_tail_probability,
                    method.winning_epoch_sample,
                    method.winning_coarse_cfo_hz,
                    method.winning_residual_cfo_hz,
                    patterns,
                    stream.evidence.exact.provenance,
                    None,
                    None,
                )
            )
    method_order = {method: index for index, method in enumerate(REPORT_METHOD_ORDER)}
    rows.sort(
        key=lambda item: (
            int(item.interval_start_utc_ns),
            str(item.radio_id),
            str(item.segment_id),
            str(item.receiver_chain_id),
            method_order[item.method],
        )
    )
    aggregates = tuple(
        _aggregate(method, tuple(item for item in rows if item.method is method))
        for method in REPORT_METHOD_ORDER
        if any(item.method is method for item in rows)
    )
    return RecordingStarlinkSurrogateNullViewV0_1(
        SchemaRef(RecordingStarlinkSurrogateNullViewV0_1.SCHEMA_ID, V0_1),
        bundle.recording_id,
        bundle.state,
        ref.artifact_ref,
        query,
        len(rows),
        tuple(rows[: query.maximum_rows]),
        aggregates,
        None,
        (
            "finite-rank-not-calibrated-p-value",
            "candidate-evidence-not-detection",
        ),
    )


def _aggregate(
    method: StarlinkDetectorMethod,
    rows: tuple[StarlinkSurrogateNullMethodRowV0_1, ...],
) -> StarlinkSurrogateNullMethodAggregateV0_1:
    surrogate_scores = tuple(score for row in rows for score in row.surrogate_scores)
    return StarlinkSurrogateNullMethodAggregateV0_1(
        method,
        len(rows),
        math.fsum(row.qin_score for row in rows) / len(rows),
        math.fsum(surrogate_scores) / len(surrogate_scores),
        math.fsum(row.finite_upper_tail_rank for row in rows) / len(rows),
        sum(row.qin_score > max(row.surrogate_scores) for row in rows),
        "finite-paired-upper-tail-rank-not-calibrated-p-value",
    )
