"""Authoritative FeatureSet publication and exact-reference reading."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import Digest, canonical_digest
from leo_flow.contracts.features import (
    FeatureSetBundle,
    FeatureSetRef,
    RecordingAnalysisRequest,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .codec import (
    FEATURE_SET_FORMAT_ID,
    FEATURE_SET_MEDIA_TYPE,
    MAX_FEATURE_SET_BYTES,
    decode_feature_set,
    encode_feature_set,
)


class FeatureSetPersistenceError(RuntimeError):
    """Base error for invalid, missing, or contradictory durable feature state."""


class FeatureSetNotFoundError(FeatureSetPersistenceError):
    pass


class FeatureSetIntegrityError(FeatureSetPersistenceError):
    pass


@dataclass(frozen=True)
class FeatureSetCatalogProjection:
    feature_set_id: str
    analysis_run_id: str
    recording_id: str
    input_recording_digest: Digest
    request_digest: Digest
    observation_count: int
    method_score_count: int


@dataclass(frozen=True)
class CatalogedFeatureSet:
    projection: FeatureSetCatalogProjection
    bundle_ref: ObjectRef

    @property
    def ref(self) -> FeatureSetRef:
        from leo_flow.contracts.core import AnalysisRunId, FeatureSetId

        return FeatureSetRef(
            FeatureSetId(self.projection.feature_set_id),
            AnalysisRunId(self.projection.analysis_run_id),
            self.bundle_ref,
        )


class FeatureSetCatalog(Protocol):
    def publish(
        self,
        projection: FeatureSetCatalogProjection,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> FeatureSetRef: ...

    def get(self, ref: FeatureSetRef) -> CatalogedFeatureSet | None: ...


class _BlobStore(BlobWriter, BlobReader, Protocol):
    pass


@dataclass(frozen=True)
class DurableFeatureSetView:
    _ref: FeatureSetRef
    _bundle: FeatureSetBundle

    @property
    def ref(self) -> FeatureSetRef:
        return self._ref

    def bundle(self) -> FeatureSetBundle:
        return self._bundle


class DurableFeatureSetRepository:
    """Publish one bundle blob, then expose one immutable catalog identity."""

    def __init__(self, blobs: _BlobStore, catalog: FeatureSetCatalog) -> None:
        self._blobs = blobs
        self._catalog = catalog

    def publish(
        self,
        request: RecordingAnalysisRequest,
        bundle: FeatureSetBundle,
        *,
        idempotency_key: str,
    ) -> FeatureSetRef:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        projection = feature_set_projection(request, bundle)
        payload = encode_feature_set(bundle)
        bundle_ref = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=FEATURE_SET_MEDIA_TYPE,
            format_id=FEATURE_SET_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:feature-bundle",
        )
        return self._catalog.publish(
            projection,
            bundle_ref,
            request.recording_object_ref,
            idempotency_key=idempotency_key,
        )

    def open(self, ref: FeatureSetRef) -> AbstractContextManager[DurableFeatureSetView]:
        return self._open(ref)

    @contextmanager
    def _open(self, ref: FeatureSetRef) -> Iterator[DurableFeatureSetView]:
        cataloged = self._catalog.get(ref)
        if cataloged is None:
            raise FeatureSetNotFoundError(
                "no feature set exactly matches the requested reference"
            )
        bundle_ref = cataloged.bundle_ref
        if (
            bundle_ref != ref.bundle_ref
            or bundle_ref.media_type != FEATURE_SET_MEDIA_TYPE
            or bundle_ref.format_id != FEATURE_SET_FORMAT_ID
            or bundle_ref.byte_count > MAX_FEATURE_SET_BYTES
        ):
            raise FeatureSetIntegrityError("feature bundle metadata is invalid")
        metadata = self._blobs.head(bundle_ref)
        if metadata.ref != bundle_ref or not metadata.verified:
            raise FeatureSetIntegrityError(
                "blob store did not verify exact feature bundle metadata"
            )
        with self._blobs.open(bundle_ref) as stream:
            payload = stream.read(MAX_FEATURE_SET_BYTES + 1)
        if (
            len(payload) != bundle_ref.byte_count
            or Digest.sha256(payload) != bundle_ref.digest
        ):
            raise FeatureSetIntegrityError(
                "feature bundle bytes do not match catalog metadata"
            )
        try:
            bundle = decode_feature_set(payload)
        except ValueError as error:
            raise FeatureSetIntegrityError(
                "feature bundle bytes are invalid"
            ) from error
        if (
            bundle.feature_set_id != ref.feature_set_id
            or bundle.analysis_run_id != ref.analysis_run_id
            or _bundle_projection(bundle, cataloged.projection.request_digest)
            != cataloged.projection
        ):
            raise FeatureSetIntegrityError(
                "authoritative feature bundle disagrees with catalog projection"
            )
        yield DurableFeatureSetView(ref, bundle)


def feature_set_projection(
    request: RecordingAnalysisRequest, bundle: FeatureSetBundle
) -> FeatureSetCatalogProjection:
    """Validate the complete publication closure and derive its projection."""

    if request.requested_output_schema != bundle.schema:
        raise FeatureSetIntegrityError("request does not select the bundle schema")
    recording_digest = request.recording_object_ref.identity_digest()
    if bundle.recording_id != request.recording_id:
        raise FeatureSetIntegrityError("bundle belongs to another recording")
    if bundle.input_recording_identity_digest != recording_digest:
        raise FeatureSetIntegrityError("bundle input recording identity differs")
    provenance = bundle.provenance
    if provenance.normalized_config_digest != request.config_ref.digest:
        raise FeatureSetIntegrityError("bundle configuration provenance differs")
    if provenance.input_digests != (recording_digest,):
        raise FeatureSetIntegrityError("bundle input provenance differs")
    dependency_refs = tuple(
        sorted(
            request.dependency_refs,
            key=lambda item: (item.artifact_id, str(item.digest)),
        )
    )
    if len({item.artifact_id for item in dependency_refs}) != len(dependency_refs):
        raise FeatureSetIntegrityError("request dependency IDs are duplicated")
    expected_dependencies = (request.algorithm_ref.digest,) + tuple(
        item.digest for item in dependency_refs
    )
    if provenance.dependency_digests != expected_dependencies:
        raise FeatureSetIntegrityError("bundle dependency provenance differs")
    feature_ids = [item.feature_id for item in bundle.observations]
    if len(feature_ids) != len(set(feature_ids)):
        raise FeatureSetIntegrityError("bundle feature IDs are duplicated")
    return _bundle_projection(bundle, canonical_digest(request))


def _bundle_projection(
    bundle: FeatureSetBundle, request_digest: Digest
) -> FeatureSetCatalogProjection:
    return FeatureSetCatalogProjection(
        feature_set_id=str(bundle.feature_set_id),
        analysis_run_id=str(bundle.analysis_run_id),
        recording_id=str(bundle.recording_id),
        input_recording_digest=bundle.input_recording_identity_digest,
        request_digest=request_digest,
        observation_count=len(bundle.observations),
        method_score_count=len(bundle.method_scores),
    )
