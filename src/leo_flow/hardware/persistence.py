"""CAS-first publication and exact reading of hardware metadata."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import Digest
from leo_flow.contracts.hardware import (
    HardwareMetadataSnapshot,
    HardwareMetadataSnapshotRef,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .codec import (
    HARDWARE_SNAPSHOT_FORMAT_ID,
    HARDWARE_SNAPSHOT_MEDIA_TYPE,
    MAX_HARDWARE_SNAPSHOT_BYTES,
    decode_hardware_snapshot,
    encode_hardware_snapshot,
)


class HardwareSnapshotPersistenceError(RuntimeError):
    pass


class HardwareSnapshotNotFoundError(HardwareSnapshotPersistenceError):
    pass


class HardwareSnapshotIntegrityError(HardwareSnapshotPersistenceError):
    pass


@dataclass(frozen=True)
class HardwareChainProjection:
    chain_index: int
    receiver_chain_id: str
    radio_id: str
    radio_channel: int
    lnb_id: str
    polarization: str | None
    cable_id: str | None
    valid_from_utc_ns: int
    valid_until_utc_ns: int | None


@dataclass(frozen=True)
class HardwareSnapshotProjection:
    ref: HardwareMetadataSnapshotRef
    station_id: str
    radio_ids: tuple[str, ...]
    chains: tuple[HardwareChainProjection, ...]


@dataclass(frozen=True)
class CatalogedHardwareSnapshot:
    projection: HardwareSnapshotProjection
    bundle_ref: ObjectRef


class HardwareSnapshotCatalog(Protocol):
    def publish(
        self,
        projection: HardwareSnapshotProjection,
        bundle_ref: ObjectRef,
        *,
        idempotency_key: str,
    ) -> HardwareMetadataSnapshotRef: ...

    def get(
        self, ref: HardwareMetadataSnapshotRef
    ) -> CatalogedHardwareSnapshot | None: ...


class _BlobStore(BlobWriter, BlobReader, Protocol):
    pass


class DurableHardwareMetadataRepository:
    def __init__(self, blobs: _BlobStore, catalog: HardwareSnapshotCatalog) -> None:
        self._blobs = blobs
        self._catalog = catalog

    def publish(
        self, snapshot: HardwareMetadataSnapshot, *, idempotency_key: str
    ) -> HardwareMetadataSnapshotRef:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        payload = encode_hardware_snapshot(snapshot)
        digest = Digest.sha256(payload)
        projection = hardware_snapshot_projection(snapshot, digest)
        bundle_ref = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=digest,
            expected_bytes=len(payload),
            media_type=HARDWARE_SNAPSHOT_MEDIA_TYPE,
            format_id=HARDWARE_SNAPSHOT_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:hardware-bundle",
        )
        return self._catalog.publish(
            projection, bundle_ref, idempotency_key=idempotency_key
        )

    def get(self, ref: HardwareMetadataSnapshotRef) -> HardwareMetadataSnapshot:
        cataloged = self._catalog.get(ref)
        if cataloged is None:
            raise HardwareSnapshotNotFoundError(
                "no hardware snapshot exactly matches the requested reference"
            )
        bundle_ref = cataloged.bundle_ref
        if (
            bundle_ref.digest != ref.digest
            or bundle_ref.media_type != HARDWARE_SNAPSHOT_MEDIA_TYPE
            or bundle_ref.format_id != HARDWARE_SNAPSHOT_FORMAT_ID
            or bundle_ref.byte_count > MAX_HARDWARE_SNAPSHOT_BYTES
        ):
            raise HardwareSnapshotIntegrityError("hardware bundle metadata is invalid")
        metadata = self._blobs.head(bundle_ref)
        if metadata.ref != bundle_ref or not metadata.verified:
            raise HardwareSnapshotIntegrityError(
                "blob store did not verify exact hardware bundle metadata"
            )
        with self._blobs.open(bundle_ref) as stream:
            payload = stream.read(MAX_HARDWARE_SNAPSHOT_BYTES + 1)
        if (
            len(payload) != bundle_ref.byte_count
            or Digest.sha256(payload) != ref.digest
        ):
            raise HardwareSnapshotIntegrityError(
                "hardware bundle bytes do not match catalog metadata"
            )
        try:
            snapshot = decode_hardware_snapshot(payload)
        except ValueError as error:
            raise HardwareSnapshotIntegrityError(
                "hardware bundle bytes are invalid"
            ) from error
        if hardware_snapshot_projection(snapshot, ref.digest) != cataloged.projection:
            raise HardwareSnapshotIntegrityError(
                "hardware bundle disagrees with catalog projection"
            )
        return snapshot


def hardware_snapshot_projection(
    snapshot: HardwareMetadataSnapshot, digest: Digest
) -> HardwareSnapshotProjection:
    return HardwareSnapshotProjection(
        HardwareMetadataSnapshotRef(snapshot.snapshot_id, digest),
        str(snapshot.station_id),
        tuple(str(value) for value in snapshot.radio_ids),
        tuple(
            HardwareChainProjection(
                index,
                str(chain.receiver_chain_id),
                str(chain.radio_id),
                chain.radio_channel,
                chain.lnb_id,
                chain.polarization,
                chain.cable_id,
                int(chain.valid_from_utc_ns),
                (
                    None
                    if chain.valid_until_utc_ns is None
                    else int(chain.valid_until_utc_ns)
                ),
            )
            for index, chain in enumerate(snapshot.receiver_chains)
        ),
    )
