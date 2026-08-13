"""Whole-bundle persistence orchestration for durable dataset snapshots."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Protocol, cast

from leo_flow.contracts.core import Digest, DigestAlgorithm, canonical_json_bytes
from leo_flow.contracts.storage import ObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .codec import (
    MAX_DATASET_SNAPSHOT_BYTES,
    decode_dataset_snapshot,
    encode_dataset_snapshot,
)
from .snapshot import DatasetSnapshotBundle, DatasetSnapshotRef, verify_snapshot_ref

DATASET_SNAPSHOT_MEDIA_TYPE = "application/json"
DATASET_SNAPSHOT_FORMAT_ID = "dataset-snapshot-bundle-v0.1"


class DatasetSnapshotPersistenceError(RuntimeError):
    """A durable snapshot is missing, corrupt, or contradicts its projection."""


class DatasetSnapshotNotFoundError(DatasetSnapshotPersistenceError):
    """No catalog entry exactly matches the requested snapshot reference."""


class DatasetSnapshotIntegrityError(DatasetSnapshotPersistenceError):
    """Catalog, blob metadata, bytes, and scientific identity do not agree."""


@dataclass(frozen=True)
class DatasetMemberProjection:
    member_index: int
    feature_set_id: str
    analysis_run_id: str
    feature_digest: Digest
    feature_byte_count: int
    feature_media_type: str
    feature_format_id: str
    feature_locator: str
    split_group_id: str
    split: str
    role: str
    truth_json: bytes


@dataclass(frozen=True)
class DatasetSnapshotProjection:
    ref: DatasetSnapshotRef
    evaluated_method_id: str
    selection_spec: str
    selection_cutoff_utc_ns: int
    promoted: bool
    promotion_warnings: tuple[str, ...]
    members: tuple[DatasetMemberProjection, ...]


@dataclass(frozen=True)
class CatalogedDatasetSnapshot:
    projection: DatasetSnapshotProjection
    bundle_ref: ObjectRef


class DatasetSnapshotCatalog(Protocol):
    def publish(
        self,
        snapshot: DatasetSnapshotBundle,
        bundle_ref: ObjectRef,
        *,
        idempotency_key: str,
    ) -> DatasetSnapshotRef: ...

    def get(self, ref: DatasetSnapshotRef) -> CatalogedDatasetSnapshot | None: ...


class _BlobStore(BlobWriter, BlobReader, Protocol):
    pass


class DurableDatasetSnapshotRepository:
    """Publish and read exactly one canonical blob through an immutable catalog."""

    def __init__(
        self,
        blobs: _BlobStore,
        catalog: DatasetSnapshotCatalog,
    ) -> None:
        self._blobs = blobs
        self._catalog = catalog

    def publish(
        self, snapshot: DatasetSnapshotBundle, *, idempotency_key: str
    ) -> DatasetSnapshotRef:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        payload = encode_dataset_snapshot(snapshot)
        digest = Digest.sha256(payload)
        bundle_ref = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=digest,
            expected_bytes=len(payload),
            media_type=DATASET_SNAPSHOT_MEDIA_TYPE,
            format_id=DATASET_SNAPSHOT_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:dataset-bundle",
        )
        return self._catalog.publish(
            snapshot, bundle_ref, idempotency_key=idempotency_key
        )

    def get(self, ref: DatasetSnapshotRef) -> DatasetSnapshotBundle:
        cataloged = self._catalog.get(ref)
        if cataloged is None:
            raise DatasetSnapshotNotFoundError(
                "no dataset snapshot exactly matches the requested reference"
            )
        bundle_ref = cataloged.bundle_ref
        if (
            bundle_ref.media_type != DATASET_SNAPSHOT_MEDIA_TYPE
            or bundle_ref.format_id != DATASET_SNAPSHOT_FORMAT_ID
            or bundle_ref.byte_count > MAX_DATASET_SNAPSHOT_BYTES
        ):
            raise DatasetSnapshotIntegrityError("dataset bundle metadata is invalid")
        metadata = self._blobs.head(bundle_ref)
        if metadata.ref != bundle_ref or not metadata.verified:
            raise DatasetSnapshotIntegrityError(
                "blob store did not verify exact dataset bundle metadata"
            )
        with self._blobs.open(bundle_ref) as stream:
            payload = stream.read(MAX_DATASET_SNAPSHOT_BYTES + 1)
        if (
            len(payload) != bundle_ref.byte_count
            or Digest.sha256(payload) != bundle_ref.digest
        ):
            raise DatasetSnapshotIntegrityError(
                "dataset bundle bytes do not match catalog metadata"
            )
        try:
            snapshot = decode_dataset_snapshot(payload)
            verify_snapshot_ref(snapshot, ref)
        except ValueError as error:
            raise DatasetSnapshotIntegrityError(
                "dataset bundle does not match its requested reference"
            ) from error
        if dataset_snapshot_projection(snapshot) != cataloged.projection:
            raise DatasetSnapshotIntegrityError(
                "authoritative dataset bundle disagrees with catalog projection"
            )
        return snapshot


def dataset_snapshot_projection(
    snapshot: DatasetSnapshotBundle,
) -> DatasetSnapshotProjection:
    """Create the exact normalized catalog projection from canonical bytes."""

    document = cast(dict[str, object], json.loads(encode_dataset_snapshot(snapshot)))
    raw_members = cast(list[dict[str, object]], document["members"])
    members: list[DatasetMemberProjection] = []
    for index, member in enumerate(raw_members):
        feature_ref = cast(dict[str, object], member["feature_set_ref"])
        bundle_ref = cast(dict[str, object], feature_ref["bundle_ref"])
        digest_text = cast(str, bundle_ref["digest"])
        algorithm, value = digest_text.split(":", 1)
        if algorithm != "sha256":
            raise ValueError("dataset feature projection requires sha256")
        members.append(
            DatasetMemberProjection(
                member_index=index,
                feature_set_id=cast(str, feature_ref["feature_set_id"]),
                analysis_run_id=cast(str, feature_ref["analysis_run_id"]),
                feature_digest=Digest(DigestAlgorithm.SHA256, value),
                feature_byte_count=cast(int, bundle_ref["byte_count"]),
                feature_media_type=cast(str, bundle_ref["media_type"]),
                feature_format_id=cast(str, bundle_ref["format_id"]),
                feature_locator=cast(str, bundle_ref["locator"]),
                split_group_id=cast(str, member["split_group_id"]),
                split=cast(str, member["split"]),
                role=cast(str, member["role"]),
                truth_json=canonical_json_bytes(member["truth"]),
            )
        )
    feature_dataset = snapshot.feature_dataset
    return DatasetSnapshotProjection(
        ref=snapshot.ref,
        evaluated_method_id=snapshot.evaluated_method_id,
        selection_spec=feature_dataset.selection_spec,
        selection_cutoff_utc_ns=int(feature_dataset.selection_cutoff_utc_ns),
        promoted=snapshot.promoted,
        promotion_warnings=snapshot.promotion_warnings,
        members=tuple(members),
    )
