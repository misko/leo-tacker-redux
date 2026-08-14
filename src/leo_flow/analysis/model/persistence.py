"""Authoritative ModelSnapshot publication, reading, and explicit release."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import Digest, canonical_digest
from leo_flow.contracts.model import (
    ModelAnalysisRequest,
    ModelApproval,
    ModelRelease,
    ModelSnapshotBundle,
    ModelSnapshotRef,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .codec import (
    MAX_MODEL_SNAPSHOT_BYTES,
    MODEL_SNAPSHOT_FORMAT_ID,
    MODEL_SNAPSHOT_MEDIA_TYPE,
    decode_model_snapshot,
    encode_model_snapshot,
)


class ModelSnapshotPersistenceError(RuntimeError):
    pass


class ModelSnapshotNotFoundError(ModelSnapshotPersistenceError):
    pass


class ModelSnapshotIntegrityError(ModelSnapshotPersistenceError):
    pass


@dataclass(frozen=True)
class ModelSnapshotCatalogProjection:
    model_snapshot_id: str
    model_run_id: str
    dataset_snapshot_id: str
    dataset_membership_digest: Digest
    request_digest: Digest
    provenance_digest: Digest
    parameter_count: int


@dataclass(frozen=True)
class CatalogedModelSnapshot:
    projection: ModelSnapshotCatalogProjection
    bundle_ref: ObjectRef

    @property
    def ref(self) -> ModelSnapshotRef:
        from leo_flow.contracts.core import ModelRunId, ModelSnapshotId

        return ModelSnapshotRef(
            ModelSnapshotId(self.projection.model_snapshot_id),
            ModelRunId(self.projection.model_run_id),
            self.bundle_ref,
        )


class ModelSnapshotCatalog(Protocol):
    def publish(
        self,
        projection: ModelSnapshotCatalogProjection,
        bundle_ref: ObjectRef,
        request: ModelAnalysisRequest,
        bundle: ModelSnapshotBundle,
        *,
        idempotency_key: str,
    ) -> ModelSnapshotRef: ...

    def get(self, ref: ModelSnapshotRef) -> CatalogedModelSnapshot | None: ...

    def release(
        self,
        model_ref: ModelSnapshotRef,
        alias: str,
        approval: ModelApproval,
        *,
        idempotency_key: str,
    ) -> ModelRelease: ...

    def get_release(self, alias: str) -> ModelRelease | None: ...


class _BlobStore(BlobWriter, BlobReader, Protocol):
    pass


@dataclass(frozen=True)
class DurableModelSnapshotView:
    _ref: ModelSnapshotRef
    _bundle: ModelSnapshotBundle

    @property
    def ref(self) -> ModelSnapshotRef:
        return self._ref

    def bundle(self) -> ModelSnapshotBundle:
        return self._bundle


class DurableModelSnapshotRepository:
    """CAS-first immutable publication with PostgreSQL as visibility point."""

    def __init__(self, blobs: _BlobStore, catalog: ModelSnapshotCatalog) -> None:
        self._blobs = blobs
        self._catalog = catalog

    def publish(
        self,
        request: ModelAnalysisRequest,
        bundle: ModelSnapshotBundle,
        *,
        idempotency_key: str,
    ) -> ModelSnapshotRef:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        projection = model_snapshot_projection(request, bundle)
        payload = encode_model_snapshot(bundle)
        bundle_ref = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=MODEL_SNAPSHOT_MEDIA_TYPE,
            format_id=MODEL_SNAPSHOT_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:model-bundle",
        )
        return self._catalog.publish(
            projection,
            bundle_ref,
            request,
            bundle,
            idempotency_key=idempotency_key,
        )

    def open(
        self, ref: ModelSnapshotRef
    ) -> AbstractContextManager[DurableModelSnapshotView]:
        return self._open(ref)

    @contextmanager
    def _open(self, ref: ModelSnapshotRef) -> Iterator[DurableModelSnapshotView]:
        cataloged = self._catalog.get(ref)
        if cataloged is None:
            raise ModelSnapshotNotFoundError(
                "no model snapshot exactly matches the requested reference"
            )
        bundle_ref = cataloged.bundle_ref
        if (
            bundle_ref != ref.bundle_ref
            or bundle_ref.media_type != MODEL_SNAPSHOT_MEDIA_TYPE
            or bundle_ref.format_id != MODEL_SNAPSHOT_FORMAT_ID
            or bundle_ref.byte_count > MAX_MODEL_SNAPSHOT_BYTES
        ):
            raise ModelSnapshotIntegrityError("model bundle metadata is invalid")
        metadata = self._blobs.head(bundle_ref)
        if metadata.ref != bundle_ref or not metadata.verified:
            raise ModelSnapshotIntegrityError(
                "blob store did not verify exact model bundle metadata"
            )
        with self._blobs.open(bundle_ref) as stream:
            payload = stream.read(MAX_MODEL_SNAPSHOT_BYTES + 1)
        if (
            len(payload) != bundle_ref.byte_count
            or Digest.sha256(payload) != bundle_ref.digest
        ):
            raise ModelSnapshotIntegrityError(
                "model bundle bytes do not match catalog metadata"
            )
        try:
            bundle = decode_model_snapshot(payload)
        except ValueError as error:
            raise ModelSnapshotIntegrityError(
                "model bundle bytes are invalid"
            ) from error
        if (
            bundle.model_snapshot_id != ref.model_snapshot_id
            or bundle.model_run_id != ref.model_run_id
            or _bundle_projection(
                bundle,
                cataloged.projection.dataset_snapshot_id,
                cataloged.projection.request_digest,
            )
            != cataloged.projection
        ):
            raise ModelSnapshotIntegrityError(
                "authoritative model bundle disagrees with catalog projection"
            )
        yield DurableModelSnapshotView(ref, bundle)

    def release(
        self,
        model_ref: ModelSnapshotRef,
        alias: str,
        approval: ModelApproval,
        *,
        idempotency_key: str,
    ) -> ModelRelease:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        return self._catalog.release(
            model_ref,
            alias,
            approval,
            idempotency_key=idempotency_key,
        )

    def get_release(self, alias: str) -> ModelRelease | None:
        return self._catalog.get_release(alias)


def model_snapshot_projection(
    request: ModelAnalysisRequest, bundle: ModelSnapshotBundle
) -> ModelSnapshotCatalogProjection:
    if (
        bundle.dataset_membership_digest
        != request.dataset_snapshot_ref.membership_digest
    ):
        raise ModelSnapshotIntegrityError("bundle dataset membership differs")
    expected_hardware = tuple(
        ref.digest
        for ref in sorted(
            request.hardware_metadata_snapshot_refs,
            key=lambda ref: (str(ref.snapshot_id), str(ref.digest)),
        )
    )
    if len({ref.snapshot_id for ref in request.hardware_metadata_snapshot_refs}) != len(
        request.hardware_metadata_snapshot_refs
    ):
        raise ModelSnapshotIntegrityError(
            "request hardware snapshot IDs are duplicated"
        )
    if bundle.hardware_snapshot_digests != expected_hardware:
        raise ModelSnapshotIntegrityError("bundle hardware provenance differs")
    ephemerides = tuple(
        sorted(
            request.ephemeris_snapshot_refs,
            key=lambda ref: (ref.source.value, str(ref.snapshot_id)),
        )
    )
    if len({ref.snapshot_id for ref in ephemerides}) != len(ephemerides):
        raise ModelSnapshotIntegrityError(
            "request ephemeris snapshot IDs are duplicated"
        )
    expected_ephemerides = tuple(ref.normalized_digest for ref in ephemerides)
    if bundle.ephemeris_snapshot_digests != expected_ephemerides:
        raise ModelSnapshotIntegrityError("bundle ephemeris provenance differs")
    provenance = bundle.provenance
    if provenance.normalized_config_digest != request.model_config_ref.digest:
        raise ModelSnapshotIntegrityError("bundle configuration provenance differs")
    if (
        not provenance.input_digests
        or provenance.input_digests[0] != request.dataset_snapshot_ref.membership_digest
        or len(set(provenance.input_digests)) != len(provenance.input_digests)
    ):
        raise ModelSnapshotIntegrityError("bundle input provenance is not closed")
    expected_dependencies = (
        (request.algorithm_ref.digest,)
        + expected_hardware
        + tuple(
            digest
            for ref in ephemerides
            for digest in (ref.raw_digest, ref.normalized_digest)
        )
    )
    if provenance.dependency_digests != expected_dependencies:
        raise ModelSnapshotIntegrityError("bundle dependency provenance differs")
    parameter_keys = [
        (parameter.parameter_id, parameter.subject_id)
        for parameter in bundle.parameters
    ]
    if len(parameter_keys) != len(set(parameter_keys)):
        raise ModelSnapshotIntegrityError("bundle parameter identities are duplicated")
    return _bundle_projection(
        bundle,
        str(request.dataset_snapshot_ref.snapshot_id),
        canonical_digest(request),
    )


def _bundle_projection(
    bundle: ModelSnapshotBundle, dataset_snapshot_id: str, request_digest: Digest
) -> ModelSnapshotCatalogProjection:
    return ModelSnapshotCatalogProjection(
        model_snapshot_id=str(bundle.model_snapshot_id),
        model_run_id=str(bundle.model_run_id),
        dataset_snapshot_id=dataset_snapshot_id,
        dataset_membership_digest=bundle.dataset_membership_digest,
        request_digest=request_digest,
        provenance_digest=canonical_digest(bundle.provenance),
        parameter_count=len(bundle.parameters),
    )
