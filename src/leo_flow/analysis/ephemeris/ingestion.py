"""Provider-neutral retrieval, normalization, validation, and publication flow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    EphemerisSnapshotId,
    SchemaRef,
    canonical_json_bytes,
)
from leo_flow.contracts.ephemeris import (
    EphemerisRetrievalRequest,
    EphemerisSnapshot,
    RetrievalResult,
)
from leo_flow.contracts.storage import ObjectRef

from .catalog import ArchivedEphemerisSnapshot, EphemerisSnapshotCatalog
from .normalization import TLECatalogNormalizer, TLEValidator


class EphemerisRetriever(Protocol):
    @property
    def provenance_query(self) -> str: ...

    def fetch(self, request: EphemerisRetrievalRequest) -> RetrievalResult: ...


class ProvenanceArchive(Protocol):
    def put_manifest(self, data: bytes) -> ObjectRef: ...


class InvalidEphemerisCandidateError(RuntimeError):
    def __init__(self, reason_codes: tuple[str, ...]) -> None:
        super().__init__("ephemeris validation failed: " + ", ".join(reason_codes))
        self.reason_codes = reason_codes


@dataclass(frozen=True)
class EphemerisIngestionConfig:
    parser_ref: ArtifactRef
    validation_policy_ref: ArtifactRef


class EphemerisIngestionService:
    """Publish one immutable snapshot; retrieval IDs are idempotency keys."""

    def __init__(
        self,
        retriever: EphemerisRetriever,
        normalizer: TLECatalogNormalizer,
        validator: TLEValidator,
        archive: ProvenanceArchive,
        catalog: EphemerisSnapshotCatalog,
        config: EphemerisIngestionConfig,
    ) -> None:
        self._retriever = retriever
        self._normalizer = normalizer
        self._validator = validator
        self._archive = archive
        self._catalog = catalog
        self._config = config

    def acquire(
        self, request: EphemerisRetrievalRequest
    ) -> ArchivedEphemerisSnapshot:
        prior = self._catalog.get_by_retrieval(request.retrieval_id)
        if prior is not None:
            expected_request_digest = _request_digest(
                request, self._retriever.provenance_query
            ).value
            if prior.request_spec_digest != expected_request_digest:
                raise ValueError("retrieval ID was reused for a different request")
            return prior

        retrieval = self._retriever.fetch(request)
        if (
            retrieval.retrieval_id != request.retrieval_id
            or retrieval.source is not request.source
        ):
            raise ValueError("retriever returned identity for another request")
        candidate = self._normalizer.normalize(
            retrieval.raw_object_ref, self._config.parser_ref
        )
        if candidate.source is not request.source or candidate.scope != request.scope:
            raise ValueError("normalized candidate differs from requested provider/scope")
        validation = self._validator.validate(
            candidate, self._config.validation_policy_ref
        )
        if not validation.valid:
            raise InvalidEphemerisCandidateError(validation.reason_codes)

        request_digest = _request_digest(request, self._retriever.provenance_query)
        snapshot_id = _snapshot_id(request, retrieval, candidate.normalized_object_ref)
        snapshot = EphemerisSnapshot(
            SchemaRef(EphemerisSnapshot.SCHEMA_ID, V0_1),
            snapshot_id,
            request.retrieval_id,
            request.source,
            request.scope,
            retrieval.completed_utc_ns,
            retrieval.raw_object_ref,
            candidate.normalized_object_ref,
            candidate.parser_ref,
            candidate.satellite_count,
            candidate.norad_id_set_digest,
            candidate.element_epoch_min_utc_ns,
            candidate.element_epoch_max_utc_ns,
            validation,
            candidate.attribution,
        )
        manifest = canonical_json_bytes(
            {
                "schema": "org.leo-flow.ephemeris-provenance",
                "version": "1.0",
                "snapshot_id": str(snapshot_id),
                "retrieval_id": str(request.retrieval_id),
                "provider": request.source.value,
                "scope": request.scope,
                "request_spec": request.request_spec,
                "provider_query": self._retriever.provenance_query,
                "request_spec_digest": str(request_digest),
                "started_utc_ns": int(retrieval.started_utc_ns),
                "retrieved_at_utc_ns": int(retrieval.completed_utc_ns),
                "raw": _object_value(retrieval.raw_object_ref),
                "normalized": _object_value(candidate.normalized_object_ref),
                "parser": _artifact_value(candidate.parser_ref),
                "validation_policy": _artifact_value(validation.policy_ref),
                "validation_reason_codes": list(validation.reason_codes),
                "satellite_count": candidate.satellite_count,
                "norad_id_set_digest": str(candidate.norad_id_set_digest),
                "element_epoch_min_utc_ns": int(candidate.element_epoch_min_utc_ns),
                "element_epoch_max_utc_ns": int(candidate.element_epoch_max_utc_ns),
                "attribution": candidate.attribution,
            }
        )
        provenance_ref = self._archive.put_manifest(manifest)
        archived = ArchivedEphemerisSnapshot(
            snapshot, provenance_ref, request_digest.value
        )
        self._catalog.publish(archived)
        return archived


def _request_digest(
    request: EphemerisRetrievalRequest, provenance_query: str
) -> Digest:
    return Digest.sha256(
        canonical_json_bytes(
            {
                "retrieval_id": str(request.retrieval_id),
                "source": request.source.value,
                "scope": request.scope,
                "request_spec": request.request_spec,
                "provider_query": provenance_query,
            }
        )
    )


def _snapshot_id(
    request: EphemerisRetrievalRequest,
    retrieval: RetrievalResult,
    normalized_ref: ObjectRef,
) -> EphemerisSnapshotId:
    identity = Digest.sha256(
        canonical_json_bytes(
            {
                "retrieval_id": str(request.retrieval_id),
                "source": request.source.value,
                "scope": request.scope,
                "retrieved_at_utc_ns": int(retrieval.completed_utc_ns),
                "raw_digest": str(retrieval.raw_object_ref.digest),
                "normalized_digest": str(normalized_ref.digest),
            }
        )
    )
    return EphemerisSnapshotId(f"eph_{identity.value}")


def _object_value(ref: ObjectRef) -> dict[str, object]:
    return {
        "digest": str(ref.digest),
        "byte_count": ref.byte_count,
        "media_type": ref.media_type,
        "format_id": ref.format_id,
        "locator": ref.locator,
    }


def _artifact_value(ref: ArtifactRef) -> dict[str, object]:
    return {"artifact_id": ref.artifact_id, "digest": str(ref.digest)}
