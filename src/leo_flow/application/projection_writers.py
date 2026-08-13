"""Capability-scoped commands for rebuilding dashboard projections.

These commands carry the authoritative public artifact beside the deliberately
small dashboard DTO.  Infrastructure adapters must validate the closure before
making any projection visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.capture import RecordingManifest
from leo_flow.contracts.core import RadioId, canonical_digest, canonical_json_bytes
from leo_flow.contracts.dashboard import StorageHealth, TrackView
from leo_flow.contracts.features import FeatureSetBundle, FeatureSetRef
from leo_flow.contracts.model import ModelRelease, ModelSnapshotBundle, ModelSnapshotRef
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)

from .projections import ProjectionInputError


class ProjectionConflict(RuntimeError):
    """An immutable logical identity was previously projected differently."""


@dataclass(frozen=True)
class ProjectionReceipt:
    """Stable identities of rows created by, or reused for, one command."""

    projection_sequences: tuple[int, ...]


@dataclass(frozen=True)
class AuthoritativeProjectionIdentity:
    kind: str
    logical_id: str
    digest: str
    document_json: str


def authoritative_identity(
    kind: str, logical_id: str, document: object
) -> AuthoritativeProjectionIdentity:
    payload = canonical_json_bytes(document)
    return AuthoritativeProjectionIdentity(
        kind, logical_id, str(canonical_digest(document)), payload.decode("utf-8")
    )


@dataclass(frozen=True)
class RecordingProjectionCommand:
    manifest: RecordingManifest
    published_ref: PublishedRecordingRef
    recording_object_available: bool


@dataclass(frozen=True)
class FeatureProjectionCommand:
    bundle: FeatureSetBundle
    published_ref: FeatureSetRef
    recording_ref: PublishedRecordingRef


@dataclass(frozen=True)
class ModelProjectionCommand:
    bundle: ModelSnapshotBundle
    published_ref: ModelSnapshotRef


@dataclass(frozen=True)
class ModelReleaseProjectionCommand:
    bundle: ModelSnapshotBundle
    release: ModelRelease


@dataclass(frozen=True)
class TrackProjectionCommand:
    view: TrackView
    radio_id: RadioId
    model_bundle: ModelSnapshotBundle
    model_ref: ModelSnapshotRef


class CaptureProjectionWriter(Protocol):
    def project_recording(
        self, command: RecordingProjectionCommand
    ) -> ProjectionReceipt: ...


class AnalysisProjectionWriter(Protocol):
    def project_features(
        self, command: FeatureProjectionCommand
    ) -> ProjectionReceipt: ...

    def project_model(self, command: ModelProjectionCommand) -> ProjectionReceipt: ...

    def project_model_release(
        self, command: ModelReleaseProjectionCommand
    ) -> ProjectionReceipt: ...

    def project_track(self, command: TrackProjectionCommand) -> ProjectionReceipt: ...

    def project_storage_health(self, health: StorageHealth) -> ProjectionReceipt: ...


def validate_recording_command(command: RecordingProjectionCommand) -> None:
    manifest = command.manifest
    remote = command.published_ref.recording_object
    if not isinstance(command.recording_object_available, bool):
        raise ProjectionInputError("recording_object_available must be boolean")
    if manifest.recording_id != remote.recording_id:
        raise ProjectionInputError("manifest and published recording IDs differ")
    if canonical_digest(manifest) != remote.manifest_digest:
        raise ProjectionInputError("manifest does not match the published digest")


def validate_feature_command(command: FeatureProjectionCommand) -> None:
    bundle = command.bundle
    published = command.published_ref
    recording = command.recording_ref.recording_object
    _validate_bundle_object(bundle, published.bundle_ref, "feature")
    if (
        bundle.feature_set_id != published.feature_set_id
        or bundle.analysis_run_id != published.analysis_run_id
    ):
        raise ProjectionInputError("feature bundle and published reference differ")
    if bundle.recording_id != recording.recording_id:
        raise ProjectionInputError("feature and recording references differ")
    if bundle.input_recording_identity_digest != recording.identity_digest():
        raise ProjectionInputError("feature input does not identify the recording")
    feature_ids = [item.feature_id for item in bundle.observations]
    if len(feature_ids) != len(set(feature_ids)):
        raise ProjectionInputError("feature set contains duplicate feature IDs")


def validate_model_command(command: ModelProjectionCommand) -> None:
    _validate_model(command.bundle, command.published_ref)


def validate_release_command(command: ModelReleaseProjectionCommand) -> None:
    _validate_model(command.bundle, command.release.model_ref)


def validate_track_command(command: TrackProjectionCommand) -> None:
    _validate_model(command.model_bundle, command.model_ref)
    if command.view.model_snapshot_id != command.model_ref.model_snapshot_id:
        raise ProjectionInputError("track and model references differ")
    if not command.view.track_id:
        raise ProjectionInputError("track_id must be non-empty")
    if command.view.finished_utc_ns <= command.view.started_utc_ns:
        raise ProjectionInputError("track interval must be non-empty")


def validate_storage_health(health: StorageHealth) -> None:
    if not isinstance(health.available, bool):
        raise ProjectionInputError("storage availability must be boolean")
    if (health.total_bytes is None) != (health.free_bytes is None):
        raise ProjectionInputError("storage byte counts must both be present or absent")
    if health.total_bytes is not None:
        assert health.free_bytes is not None
        if (
            isinstance(health.total_bytes, bool)
            or isinstance(health.free_bytes, bool)
            or health.total_bytes < 0
            or not 0 <= health.free_bytes <= health.total_bytes
        ):
            raise ProjectionInputError("storage byte counts are invalid")


def validate_recording_ref(ref: RecordingObjectRef) -> None:
    """Expose exact-ref validation for infrastructure catalog comparisons."""
    # Construction already validates the public contract.  This named boundary
    # prevents adapters from accepting an untyped recording/catalog identifier.
    if not str(ref.recording_id):  # pragma: no cover - ContractId prevents this
        raise ProjectionInputError("recording reference is invalid")


def recording_identities(
    command: RecordingProjectionCommand,
) -> tuple[AuthoritativeProjectionIdentity, ...]:
    recording = command.published_ref.recording_object
    stable_recording = {
        "manifest_digest": str(recording.manifest_digest),
        "recording_object_identity_digest": str(recording.identity_digest()),
    }
    identities = [
        authoritative_identity(
            "recording", str(recording.recording_id), stable_recording
        )
    ]
    identities.extend(
        authoritative_identity(
            "activity",
            str(activity.activity_id),
            {"recording": stable_recording, "activity": activity},
        )
        for activity in command.manifest.activities
    )
    return tuple(identities)


def feature_identities(
    command: FeatureProjectionCommand,
) -> tuple[AuthoritativeProjectionIdentity, ...]:
    ref = command.published_ref
    document = {
        "feature_set_id": str(ref.feature_set_id),
        "analysis_run_id": str(ref.analysis_run_id),
        "bundle_ref": _stable_object_ref(ref.bundle_ref),
    }
    return tuple(
        authoritative_identity("feature", str(item.feature_id), document)
        for item in command.bundle.observations
    )


def model_identity(command: ModelProjectionCommand) -> AuthoritativeProjectionIdentity:
    ref = command.published_ref
    return authoritative_identity(
        "model",
        str(ref.model_snapshot_id),
        {
            "model_snapshot_id": str(ref.model_snapshot_id),
            "model_run_id": str(ref.model_run_id),
            "bundle_ref": _stable_object_ref(ref.bundle_ref),
        },
    )


def release_identity(
    command: ModelReleaseProjectionCommand,
) -> AuthoritativeProjectionIdentity:
    release = command.release
    return authoritative_identity(
        "release",
        release.alias,
        {
            "alias": release.alias,
            "model_ref": _stable_model_ref(release.model_ref),
            "approval": release.approval,
        },
    )


def track_identity(command: TrackProjectionCommand) -> AuthoritativeProjectionIdentity:
    return authoritative_identity(
        "track",
        command.view.track_id,
        {
            "track": command.view,
            "radio_id": str(command.radio_id),
            "model_ref": _stable_model_ref(command.model_ref),
        },
    )


def _validate_model(bundle: ModelSnapshotBundle, published: ModelSnapshotRef) -> None:
    _validate_bundle_object(bundle, published.bundle_ref, "model")
    if (
        bundle.model_snapshot_id != published.model_snapshot_id
        or bundle.model_run_id != published.model_run_id
    ):
        raise ProjectionInputError("model bundle and published reference differ")


def _validate_bundle_object(bundle: object, ref: ObjectRef, kind: str) -> None:
    payload = canonical_json_bytes(bundle)
    if ref.digest != canonical_digest(bundle) or ref.byte_count != len(payload):
        raise ProjectionInputError(f"{kind} bundle does not match its published object")


def _stable_object_ref(ref: ObjectRef) -> dict[str, object]:
    return {
        "digest": str(ref.digest),
        "byte_count": ref.byte_count,
        "media_type": ref.media_type,
        "format_id": ref.format_id,
    }


def _stable_model_ref(ref: ModelSnapshotRef) -> dict[str, object]:
    return {
        "model_snapshot_id": str(ref.model_snapshot_id),
        "model_run_id": str(ref.model_run_id),
        "bundle_ref": _stable_object_ref(ref.bundle_ref),
    }
