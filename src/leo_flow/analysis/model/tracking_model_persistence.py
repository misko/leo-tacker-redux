"""Authoritative CAS-first persistence for joint tracking-model outputs."""

from __future__ import annotations

import io
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import BinaryIO, Protocol

from leo_flow.contracts.core import (
    Digest,
    ModelRunId,
    ModelSnapshotId,
    canonical_digest,
)
from leo_flow.contracts.storage import ObjectMetadata, ObjectRef
from leo_flow.contracts.tracking_input import TrackingInputSnapshotIdentity
from leo_flow.contracts.tracking_model_output import (
    TrackingModelSnapshotBundle,
    tracking_model_snapshot_digest,
)

from .tracking_model_codec import (
    MAX_TRACKING_MODEL_SNAPSHOT_BYTES,
    TRACKING_MODEL_SNAPSHOT_FORMAT_ID,
    TRACKING_MODEL_SNAPSHOT_MEDIA_TYPE,
    decode_tracking_model_snapshot,
    encode_tracking_model_snapshot,
)


class TrackingModelPersistenceError(RuntimeError):
    """Tracking-model publication or retrieval did not complete exactly."""


class TrackingModelNotFoundError(TrackingModelPersistenceError):
    """No catalog row exactly matches the requested output reference."""


class TrackingModelIntegrityError(TrackingModelPersistenceError):
    """Catalog, CAS metadata, bytes, or decoded output disagree."""


@dataclass(frozen=True)
class TrackingModelSnapshotRef:
    model_snapshot_id: ModelSnapshotId
    model_run_id: ModelRunId
    output_digest: Digest
    bundle_ref: ObjectRef


@dataclass(frozen=True)
class TrackingModelCatalogProjection:
    model_snapshot_id: ModelSnapshotId
    model_run_id: ModelRunId
    scientific_snapshot_digest: Digest
    run_digest: Digest
    output_digest: Digest
    evidence_digest: Digest
    provenance_digest: Digest
    tracking_input_identity: TrackingInputSnapshotIdentity
    parameter_block_count: int
    accepted_association_count: int
    rejected_association_count: int
    warning_count: int
    bundle_ref: ObjectRef

    @property
    def ref(self) -> TrackingModelSnapshotRef:
        return TrackingModelSnapshotRef(
            self.model_snapshot_id,
            self.model_run_id,
            self.output_digest,
            self.bundle_ref,
        )


class TrackingModelCatalog(Protocol):
    def publish(
        self,
        projection: TrackingModelCatalogProjection,
        *,
        idempotency_key: str,
    ) -> TrackingModelSnapshotRef: ...

    def get(
        self, ref: TrackingModelSnapshotRef
    ) -> TrackingModelCatalogProjection | None: ...


class TrackingModelBlobStore(Protocol):
    def put(
        self,
        stream: BinaryIO,
        *,
        expected_digest: Digest,
        expected_bytes: int,
        media_type: str,
        format_id: str,
        idempotency_key: str,
    ) -> ObjectRef: ...

    def head(self, ref: ObjectRef) -> ObjectMetadata: ...

    def open(self, ref: ObjectRef) -> AbstractContextManager[BinaryIO]: ...


class DurableTrackingModelRepository:
    """Store immutable bytes before making one exact catalog row visible."""

    def __init__(
        self, blobs: TrackingModelBlobStore, catalog: TrackingModelCatalog
    ) -> None:
        self._blobs = blobs
        self._catalog = catalog

    def publish(
        self,
        bundle: TrackingModelSnapshotBundle,
        *,
        idempotency_key: str,
    ) -> TrackingModelSnapshotRef:
        _require_idempotency_key(idempotency_key)
        payload = encode_tracking_model_snapshot(bundle)
        digest = Digest.sha256(payload)
        try:
            bundle_ref = self._blobs.put(
                io.BytesIO(payload),
                expected_digest=digest,
                expected_bytes=len(payload),
                media_type=TRACKING_MODEL_SNAPSHOT_MEDIA_TYPE,
                format_id=TRACKING_MODEL_SNAPSHOT_FORMAT_ID,
                idempotency_key=f"{idempotency_key}:tracking-model-bundle",
            )
        except Exception as error:
            raise TrackingModelPersistenceError(
                "tracking model CAS publication failed"
            ) from error
        _verify_bundle_ref(bundle_ref, digest=digest, byte_count=len(payload))
        projection = tracking_model_projection(bundle, bundle_ref)
        try:
            published = self._catalog.publish(
                projection, idempotency_key=idempotency_key
            )
        except Exception as error:
            raise TrackingModelPersistenceError(
                "tracking model catalog publication failed"
            ) from error
        if published != projection.ref:
            raise TrackingModelIntegrityError(
                "catalog published a substituted tracking model reference"
            )
        return published

    def get(self, ref: TrackingModelSnapshotRef) -> TrackingModelSnapshotBundle:
        try:
            projection = self._catalog.get(ref)
        except Exception as error:
            raise TrackingModelPersistenceError(
                "tracking model catalog read failed"
            ) from error
        if projection is None:
            raise TrackingModelNotFoundError(
                "no tracking model exactly matches the requested reference"
            )
        if projection.ref != ref:
            raise TrackingModelIntegrityError(
                "catalog returned a substituted tracking model reference"
            )
        bundle_ref = projection.bundle_ref
        _verify_bundle_ref(
            bundle_ref,
            digest=projection.output_digest,
            byte_count=bundle_ref.byte_count,
        )
        try:
            metadata = self._blobs.head(bundle_ref)
            if metadata.ref != bundle_ref or not metadata.verified:
                raise TrackingModelIntegrityError(
                    "blob store did not verify exact tracking model metadata"
                )
            with self._blobs.open(bundle_ref) as stream:
                payload = stream.read(MAX_TRACKING_MODEL_SNAPSHOT_BYTES + 1)
        except TrackingModelIntegrityError:
            raise
        except Exception as error:
            raise TrackingModelPersistenceError(
                "tracking model blob read failed"
            ) from error
        if (
            len(payload) != bundle_ref.byte_count
            or Digest.sha256(payload) != bundle_ref.digest
        ):
            raise TrackingModelIntegrityError(
                "tracking model bytes do not match catalog metadata"
            )
        try:
            bundle = decode_tracking_model_snapshot(payload)
        except ValueError as error:
            raise TrackingModelIntegrityError(
                "tracking model bytes are noncanonical or invalid"
            ) from error
        if tracking_model_projection(bundle, bundle_ref) != projection:
            raise TrackingModelIntegrityError(
                "decoded tracking model disagrees with catalog projection"
            )
        return bundle


def tracking_model_projection(
    bundle: TrackingModelSnapshotBundle,
    bundle_ref: ObjectRef,
) -> TrackingModelCatalogProjection:
    payload = encode_tracking_model_snapshot(bundle)
    snapshot_digest = tracking_model_snapshot_digest(
        bundle.schema,
        bundle.evidence,
        bundle.receiver_lnb_estimates,
        bundle.satellite_carrier_residual_estimates,
        bundle.joint_covariance,
        bundle.accepted_associations,
        bundle.rejected_associations,
        bundle.warnings,
    )
    run_digest = canonical_digest(
        {"snapshot_digest": snapshot_digest, "provenance": bundle.provenance}
    )
    output_digest = canonical_digest(bundle)
    _verify_bundle_ref(
        bundle_ref,
        digest=output_digest,
        byte_count=len(payload),
    )
    return TrackingModelCatalogProjection(
        bundle.model_snapshot_id,
        bundle.model_run_id,
        snapshot_digest,
        run_digest,
        output_digest,
        bundle.evidence.evidence_digest(),
        canonical_digest(bundle.provenance),
        bundle.evidence.tracking_input_identity,
        len(bundle.receiver_lnb_estimates)
        + len(bundle.satellite_carrier_residual_estimates),
        len(bundle.accepted_associations),
        len(bundle.rejected_associations),
        len(bundle.warnings),
        bundle_ref,
    )


def _verify_bundle_ref(ref: ObjectRef, *, digest: Digest, byte_count: int) -> None:
    if (
        ref.digest != digest
        or ref.byte_count != byte_count
        or not 0 < ref.byte_count <= MAX_TRACKING_MODEL_SNAPSHOT_BYTES
        or ref.media_type != TRACKING_MODEL_SNAPSHOT_MEDIA_TYPE
        or ref.format_id != TRACKING_MODEL_SNAPSHOT_FORMAT_ID
    ):
        raise TrackingModelIntegrityError(
            "tracking model bundle metadata is outside authoritative bounds"
        )


def _require_idempotency_key(value: str) -> None:
    if not value or any(character.isspace() for character in value):
        raise ValueError("idempotency_key must be a token")
