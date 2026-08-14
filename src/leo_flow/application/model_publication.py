"""Minimal in-memory peer of authoritative model publication."""

from __future__ import annotations

from leo_flow.contracts.core import Digest, canonical_json_bytes
from leo_flow.contracts.model import (
    ModelAnalysisRequest,
    ModelApproval,
    ModelRelease,
    ModelSnapshotBundle,
    ModelSnapshotRef,
)
from leo_flow.contracts.storage import ObjectRef

_MODEL_FORMAT = "model-snapshot-bundle-v0.1"


class ModelPublicationError(ValueError):
    """Publication inputs do not close over the immutable bundle."""


class ModelPublicationConflict(RuntimeError):
    """An immutable identity or idempotency key was reused differently."""


class InMemoryModelPublication:
    """Executable semantic peer of the durable canonical-bundle repository."""

    def __init__(self) -> None:
        self._bundles: dict[str, ModelSnapshotBundle] = {}
        self._published: dict[str, ModelSnapshotRef] = {}
        self._publish_idempotency: dict[str, ModelSnapshotRef] = {}
        self._releases: dict[str, ModelRelease] = {}
        self._release_idempotency: dict[str, ModelRelease] = {}

    def publish(
        self,
        request: ModelAnalysisRequest,
        bundle: ModelSnapshotBundle,
        *,
        idempotency_key: str,
    ) -> ModelSnapshotRef:
        _require_key(idempotency_key)
        self._validate_closure(request, bundle)
        payload = canonical_json_bytes(bundle)
        digest = Digest.sha256(payload)
        bundle_ref = ObjectRef(
            digest,
            len(payload),
            "application/json",
            _MODEL_FORMAT,
            f"memory://models/{digest.value}",
        )
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
        self._bundles[str(candidate.model_snapshot_id)] = bundle
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
        self._releases[alias] = candidate
        self._release_idempotency[idempotency_key] = candidate
        return candidate

    def bundle(self, ref: ModelSnapshotRef) -> ModelSnapshotBundle:
        published = self._published.get(str(ref.model_snapshot_id))
        if published != ref:
            raise ModelPublicationError("model snapshot is not exactly published")
        return self._bundles[str(ref.model_snapshot_id)]

    def get_release(self, alias: str) -> ModelRelease | None:
        return self._releases.get(alias)

    @staticmethod
    def _validate_closure(
        request: ModelAnalysisRequest,
        bundle: ModelSnapshotBundle,
    ) -> None:
        if (
            request.dataset_snapshot_ref.membership_digest
            != bundle.dataset_membership_digest
        ):
            raise ModelPublicationError(
                "request membership digest does not match the model bundle"
            )
        if (
            not bundle.provenance.input_digests
            or bundle.provenance.input_digests[0] != bundle.dataset_membership_digest
        ):
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
        ephemerides = tuple(
            sorted(
                request.ephemeris_snapshot_refs,
                key=lambda ref: (ref.source.value, str(ref.snapshot_id)),
            )
        )
        expected_ephemerides = tuple(ref.normalized_digest for ref in ephemerides)
        if expected_ephemerides != bundle.ephemeris_snapshot_digests:
            raise ModelPublicationError(
                "request ephemeris digests do not match the model bundle"
            )
        required_dependencies = (
            (request.algorithm_ref.digest,)
            + expected_hardware
            + tuple(
                digest
                for ref in ephemerides
                for digest in (ref.raw_digest, ref.normalized_digest)
            )
        )
        if bundle.provenance.dependency_digests != required_dependencies:
            raise ModelPublicationError(
                "request dependencies are absent from model provenance"
            )


def _require_key(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ModelPublicationError("idempotency_key must be non-empty")
