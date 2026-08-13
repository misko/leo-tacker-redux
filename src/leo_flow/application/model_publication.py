"""Minimal in-memory model object, publication, and explicit-release adapter."""

from __future__ import annotations

from leo_flow.contracts.core import Digest, canonical_json_bytes
from leo_flow.contracts.model import (
    ModelAnalysisRequest,
    ModelApproval,
    ModelRelease,
    ModelSnapshotBundle,
    ModelSnapshotProjection,
    ModelSnapshotRef,
)
from leo_flow.contracts.storage import ObjectRef

_MODEL_FORMAT = "model-snapshot-bundle-v0.1"


class ModelPublicationError(ValueError):
    """Publication inputs do not close over the staged immutable bundle."""


class ModelObjectNotStaged(ModelPublicationError):
    """The publisher cannot resolve the supplied object reference."""


class ModelPublicationConflict(RuntimeError):
    """An immutable identity or idempotency key was reused differently."""


class InMemoryModelPublication:
    """Implement model publication and release over canonical staged objects.

    ``ModelPublisher.publish`` receives a bundle reference and projection, but
    not the bundle or its model run ID.  A real adapter must resolve the object
    from blob storage.  ``stage`` is the deliberately small in-memory analogue
    of that blob write and makes the otherwise implicit capability visible.
    """

    def __init__(self) -> None:
        self._staged: dict[Digest, tuple[ObjectRef, ModelSnapshotBundle]] = {}
        self._published: dict[str, ModelSnapshotRef] = {}
        self._publish_idempotency: dict[str, ModelSnapshotRef] = {}
        self._releases: dict[str, ModelRelease] = {}
        self._release_idempotency: dict[str, ModelRelease] = {}

    def stage(self, bundle: ModelSnapshotBundle) -> ObjectRef:
        payload = canonical_json_bytes(bundle)
        digest = Digest.sha256(payload)
        ref = ObjectRef(
            digest=digest,
            byte_count=len(payload),
            media_type="application/json",
            format_id=_MODEL_FORMAT,
            locator=f"memory://models/{digest.value}",
        )
        existing = self._staged.get(digest)
        if existing is not None and existing != (ref, bundle):
            raise ModelPublicationConflict(
                "model object digest identifies different staged content"
            )
        self._staged[digest] = (ref, bundle)
        return ref

    def publish(
        self,
        request: ModelAnalysisRequest,
        bundle_ref: ObjectRef,
        projection: ModelSnapshotProjection,
        *,
        idempotency_key: str,
    ) -> ModelSnapshotRef:
        _require_key(idempotency_key)
        staged = self._staged.get(bundle_ref.digest)
        if staged is None:
            raise ModelObjectNotStaged("model bundle object is not staged")
        exact_ref, bundle = staged
        if exact_ref != bundle_ref:
            raise ModelPublicationError(
                "bundle_ref metadata does not match the staged object"
            )
        self._validate_closure(request, bundle, projection)
        candidate = ModelSnapshotRef(
            model_snapshot_id=bundle.model_snapshot_id,
            model_run_id=bundle.model_run_id,
            bundle_ref=bundle_ref,
        )
        by_key = self._publish_idempotency.get(idempotency_key)
        if by_key is not None:
            if by_key != candidate:
                raise ModelPublicationConflict(
                    "model idempotency key identifies a different snapshot"
                )
            return by_key
        existing = self._published.get(str(candidate.model_snapshot_id))
        if existing is not None and existing != candidate:
            raise ModelPublicationConflict(
                "model snapshot ID identifies a different run or object"
            )
        self._published[str(candidate.model_snapshot_id)] = candidate
        self._publish_idempotency[idempotency_key] = candidate
        return candidate

    def release(
        self,
        model_ref: ModelSnapshotRef,
        alias: str,
        approval: ModelApproval,
        *,
        idempotency_key: str,
    ) -> ModelRelease:
        _require_key(idempotency_key)
        published = self._published.get(str(model_ref.model_snapshot_id))
        if published != model_ref:
            raise ModelPublicationError(
                "release must reference an exactly published model snapshot"
            )
        candidate = ModelRelease(alias, model_ref, approval)
        by_key = self._release_idempotency.get(idempotency_key)
        if by_key is not None:
            if by_key != candidate:
                raise ModelPublicationConflict(
                    "release idempotency key identifies a different release"
                )
            return by_key
        existing = self._releases.get(alias)
        if existing is not None and existing != candidate:
            raise ModelPublicationConflict(
                "release alias already identifies a different approval or model"
            )
        self._releases[alias] = candidate
        self._release_idempotency[idempotency_key] = candidate
        return candidate

    def bundle(self, ref: ModelSnapshotRef) -> ModelSnapshotBundle:
        published = self._published.get(str(ref.model_snapshot_id))
        if published != ref:
            raise ModelPublicationError("model snapshot is not exactly published")
        return self._staged[ref.bundle_ref.digest][1]

    @staticmethod
    def _validate_closure(
        request: ModelAnalysisRequest,
        bundle: ModelSnapshotBundle,
        projection: ModelSnapshotProjection,
    ) -> None:
        if projection.model_snapshot_id != bundle.model_snapshot_id:
            raise ModelPublicationError("projection and bundle model IDs differ")
        if projection.parameter_count != len(bundle.parameters):
            raise ModelPublicationError(
                "projection parameter count does not match the bundle"
            )
        if (
            request.dataset_snapshot_ref.membership_digest
            != bundle.dataset_membership_digest
        ):
            raise ModelPublicationError(
                "request membership digest does not match the model bundle"
            )
        if bundle.dataset_membership_digest not in bundle.provenance.input_digests:
            raise ModelPublicationError(
                "model provenance does not close over dataset membership"
            )
        if (
            request.model_config_ref.digest
            != bundle.provenance.normalized_config_digest
        ):
            raise ModelPublicationError(
                "request configuration digest is absent from model provenance"
            )
        expected_hardware = tuple(
            ref.digest
            for ref in sorted(
                request.hardware_metadata_snapshot_refs,
                key=lambda ref: (str(ref.snapshot_id), str(ref.digest)),
            )
        )
        if expected_hardware != bundle.hardware_snapshot_digests:
            raise ModelPublicationError(
                "request hardware digests do not match the model bundle"
            )
        expected_ephemerides = tuple(
            ref.normalized_digest
            for ref in sorted(
                request.ephemeris_snapshot_refs,
                key=lambda ref: (ref.source.value, str(ref.snapshot_id)),
            )
        )
        if expected_ephemerides != bundle.ephemeris_snapshot_digests:
            raise ModelPublicationError(
                "request ephemeris digests do not match the model bundle"
            )
        required_dependencies = (
            (request.algorithm_ref.digest,)
            + expected_hardware
            + tuple(
                digest
                for ref in request.ephemeris_snapshot_refs
                for digest in (ref.raw_digest, ref.normalized_digest)
            )
        )
        missing_dependencies = tuple(
            digest
            for digest in required_dependencies
            if digest not in bundle.provenance.dependency_digests
        )
        if missing_dependencies:
            raise ModelPublicationError(
                "request dependencies are absent from model provenance"
            )


def _require_key(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ModelPublicationError("idempotency_key must be non-empty")
