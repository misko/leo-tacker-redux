"""CAS-first publication and verified reading of tracking-input snapshots."""

from __future__ import annotations

import io
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import BinaryIO, Protocol

from leo_flow.contracts.core import ArtifactRef, Digest, canonical_digest
from leo_flow.contracts.storage import ObjectMetadata, ObjectRef
from leo_flow.contracts.tracking_input import (
    TRACKING_INPUT_FORMAT_ID,
    TRACKING_INPUT_MEDIA_TYPE,
    DurableDatasetIdentity,
    TrackingInputSnapshot,
    TrackingInputSnapshotIdentity,
    TrackingInputSnapshotRef,
)

from .tracking_input_codec import (
    MAX_TRACKING_INPUT_BYTES,
    decode_tracking_input,
    encode_tracking_input,
)


class TrackingInputPersistenceError(RuntimeError):
    """Tracking input publication or retrieval did not complete exactly."""


class TrackingInputNotFoundError(TrackingInputPersistenceError):
    """No catalog row matches the requested content identity."""


class TrackingInputIntegrityError(TrackingInputPersistenceError):
    """Catalog projection, blob metadata, bytes, or snapshot disagree."""


@dataclass(frozen=True)
class TrackingInputEntryProjection:
    """Query-facing identity of one ordered tracking measurement."""

    entry_index: int
    feature_set_id: str
    analysis_run_id: str
    feature_bundle_digest: Digest
    feature_id: str
    recording_id: str
    recording_identity_digest: Digest
    receiver_chain_id: str
    midpoint_utc_ns: int
    hardware_link_id: str
    hardware_link_digest: Digest
    ephemeris_link_id: str
    ephemeris_link_digest: Digest
    calibration_ref: ArtifactRef
    prediction_policy_ref: ArtifactRef


@dataclass(frozen=True)
class TrackingInputProjection:
    """Minimal exact projection for a future relational catalog adapter."""

    ref: TrackingInputSnapshotRef
    durable_dataset: DurableDatasetIdentity
    builder_ref: ArtifactRef
    selector_ref: ArtifactRef
    provenance_digest: Digest
    entry_count: int
    entries: tuple[TrackingInputEntryProjection, ...]


@dataclass(frozen=True)
class CatalogedTrackingInput:
    projection: TrackingInputProjection


@dataclass(frozen=True)
class DurableTrackingInputView:
    """Verified snapshot with the catalog's current relocatable I/O reference."""

    ref: TrackingInputSnapshotRef
    snapshot: TrackingInputSnapshot


class TrackingInputCatalog(Protocol):
    def publish(
        self,
        projection: TrackingInputProjection,
        *,
        idempotency_key: str,
    ) -> TrackingInputSnapshotRef: ...

    def get(self, ref: TrackingInputSnapshotRef) -> CatalogedTrackingInput | None: ...

    def get_by_identity(
        self, identity: TrackingInputSnapshotIdentity
    ) -> CatalogedTrackingInput | None: ...


class TrackingInputBlobStore(Protocol):
    """Only the immutable byte capabilities required by this repository."""

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


class DurableTrackingInputRepository:
    """Publish canonical bytes before catalog visibility; verify every read."""

    def __init__(
        self, blobs: TrackingInputBlobStore, catalog: TrackingInputCatalog
    ) -> None:
        self._blobs = blobs
        self._catalog = catalog

    def publish(
        self,
        snapshot: TrackingInputSnapshot,
        *,
        idempotency_key: str,
    ) -> TrackingInputSnapshotRef:
        if not idempotency_key or any(
            character.isspace() for character in idempotency_key
        ):
            raise ValueError("idempotency_key must be a token")
        payload = encode_tracking_input(snapshot)
        digest = Digest.sha256(payload)
        try:
            bundle_ref = self._blobs.put(
                io.BytesIO(payload),
                expected_digest=digest,
                expected_bytes=len(payload),
                media_type=TRACKING_INPUT_MEDIA_TYPE,
                format_id=TRACKING_INPUT_FORMAT_ID,
                idempotency_key=f"{idempotency_key}:tracking-input-bundle",
            )
        except Exception as error:
            raise TrackingInputPersistenceError(
                "tracking input CAS publication failed"
            ) from error
        _verify_bundle_ref(bundle_ref, digest=digest, byte_count=len(payload))
        expected_ref = TrackingInputSnapshotRef(
            snapshot.snapshot_id,
            snapshot.snapshot_digest,
            snapshot.membership_digest,
            bundle_ref,
        )
        projection = tracking_input_projection(snapshot, expected_ref)
        try:
            published_ref = self._catalog.publish(
                projection,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            raise TrackingInputPersistenceError(
                "tracking input catalog publication failed"
            ) from error
        if published_ref.identity_digest() != expected_ref.identity_digest():
            raise TrackingInputIntegrityError(
                "catalog published a different tracking input identity"
            )
        return published_ref

    def get(self, ref: TrackingInputSnapshotRef) -> TrackingInputSnapshot:
        try:
            cataloged = self._catalog.get(ref)
        except Exception as error:
            raise TrackingInputPersistenceError(
                "tracking input catalog read failed"
            ) from error
        if cataloged is None:
            raise TrackingInputNotFoundError(
                "no tracking input exactly matches the requested identity"
            )
        if not cataloged.projection.ref.matches_identity(ref.identity()):
            raise TrackingInputIntegrityError(
                "catalog returned a substituted tracking input identity"
            )
        return self._read_cataloged(ref.identity(), cataloged)

    def get_by_identity(
        self, identity: TrackingInputSnapshotIdentity
    ) -> DurableTrackingInputView:
        """Resolve scientific identity to the current ref and verify exact bytes."""

        try:
            cataloged = self._catalog.get_by_identity(identity)
        except Exception as error:
            raise TrackingInputPersistenceError(
                "tracking input identity resolution failed"
            ) from error
        if cataloged is None:
            raise TrackingInputNotFoundError(
                "no tracking input exactly matches the requested identity"
            )
        catalog_ref = cataloged.projection.ref
        if not catalog_ref.matches_identity(identity):
            raise TrackingInputIntegrityError(
                "catalog resolved a substituted tracking input identity"
            )
        snapshot = self._read_cataloged(identity, cataloged)
        return DurableTrackingInputView(catalog_ref, snapshot)

    def _read_cataloged(
        self,
        identity: TrackingInputSnapshotIdentity,
        cataloged: CatalogedTrackingInput,
    ) -> TrackingInputSnapshot:
        catalog_ref = cataloged.projection.ref
        bundle_ref = catalog_ref.bundle_ref
        _verify_bundle_ref(
            bundle_ref,
            digest=identity.bundle_digest,
            byte_count=identity.bundle_byte_count,
        )
        try:
            metadata = self._blobs.head(bundle_ref)
            if metadata.ref != bundle_ref or not metadata.verified:
                raise TrackingInputIntegrityError(
                    "blob store did not verify exact tracking input metadata"
                )
            with self._blobs.open(bundle_ref) as stream:
                payload = stream.read(MAX_TRACKING_INPUT_BYTES + 1)
        except TrackingInputIntegrityError:
            raise
        except Exception as error:
            raise TrackingInputPersistenceError(
                "tracking input blob read failed"
            ) from error
        if (
            len(payload) != bundle_ref.byte_count
            or Digest.sha256(payload) != bundle_ref.digest
        ):
            raise TrackingInputIntegrityError(
                "tracking input bytes do not match catalog metadata"
            )
        try:
            snapshot = decode_tracking_input(payload)
        except ValueError as error:
            raise TrackingInputIntegrityError(
                "tracking input bytes are noncanonical or invalid"
            ) from error
        if (
            snapshot.snapshot_id != identity.snapshot_id
            or snapshot.snapshot_digest != identity.snapshot_digest
            or snapshot.membership_digest != identity.membership_digest
        ):
            raise TrackingInputIntegrityError(
                "decoded tracking input differs from requested reference"
            )
        if tracking_input_projection(snapshot, catalog_ref) != cataloged.projection:
            raise TrackingInputIntegrityError(
                "decoded tracking input disagrees with catalog projection"
            )
        return snapshot


def tracking_input_projection(
    snapshot: TrackingInputSnapshot,
    ref: TrackingInputSnapshotRef,
) -> TrackingInputProjection:
    """Derive the complete locator-independent scientific catalog projection."""

    if (
        ref.snapshot_id != snapshot.snapshot_id
        or ref.snapshot_digest != snapshot.snapshot_digest
        or ref.membership_digest != snapshot.membership_digest
    ):
        raise TrackingInputIntegrityError(
            "tracking input reference differs from snapshot content"
        )
    entries = tuple(
        TrackingInputEntryProjection(
            index,
            str(entry.feature_set.feature_set_id),
            str(entry.feature_set.analysis_run_id),
            entry.feature_set.bundle_digest,
            str(entry.measurement.feature_id),
            str(entry.measurement.recording_id),
            entry.recording_identity_digest,
            str(entry.measurement.receiver_chain_id),
            int(entry.measurement.midpoint_utc_ns),
            entry.hardware_link.link_id,
            entry.hardware_link.link_digest,
            entry.ephemeris_link.link_id,
            entry.ephemeris_link.link_digest,
            entry.calibration.calibration_ref,
            entry.prediction.policy_ref,
        )
        for index, entry in enumerate(snapshot.entries)
    )
    return TrackingInputProjection(
        ref=ref,
        durable_dataset=snapshot.durable_dataset,
        builder_ref=snapshot.builder_ref,
        selector_ref=snapshot.selector_ref,
        provenance_digest=canonical_digest(snapshot.provenance),
        entry_count=len(entries),
        entries=entries,
    )


def _verify_bundle_ref(
    ref: ObjectRef,
    *,
    digest: Digest,
    byte_count: int,
) -> None:
    if (
        ref.digest != digest
        or ref.byte_count != byte_count
        or ref.byte_count == 0
        or ref.byte_count > MAX_TRACKING_INPUT_BYTES
        or ref.media_type != TRACKING_INPUT_MEDIA_TYPE
        or ref.format_id != TRACKING_INPUT_FORMAT_ID
    ):
        raise TrackingInputIntegrityError("tracking input bundle metadata is invalid")
