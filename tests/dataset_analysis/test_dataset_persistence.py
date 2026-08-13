from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.dataset import (
    DatasetSnapshotIntegrityError,
    DurableDatasetSnapshotRepository,
    encode_dataset_snapshot,
)
from leo_flow.analysis.dataset.persistence import (
    CatalogedDatasetSnapshot,
    DatasetSnapshotCatalog,
    dataset_snapshot_projection,
)
from leo_flow.analysis.dataset.snapshot import (
    DatasetSnapshotBundle,
    DatasetSnapshotRef,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.storage.filesystem import FileSystemBlobStore

from .test_dataset_snapshot import _snapshot


class _MemoryCatalog(DatasetSnapshotCatalog):
    def __init__(self) -> None:
        self.entry: CatalogedDatasetSnapshot | None = None
        self.publish_calls = 0

    def publish(
        self,
        snapshot: DatasetSnapshotBundle,
        bundle_ref: ObjectRef,
        *,
        idempotency_key: str,
    ) -> DatasetSnapshotRef:
        self.publish_calls += 1
        entry = CatalogedDatasetSnapshot(
            dataset_snapshot_projection(snapshot), bundle_ref
        )
        if self.entry is not None and self.entry != entry:
            raise RuntimeError("conflict")
        self.entry = entry
        return snapshot.ref

    def get(self, ref: DatasetSnapshotRef) -> CatalogedDatasetSnapshot | None:
        if self.entry is None or self.entry.projection.ref != ref:
            return None
        return self.entry


class _UnverifiedBlobStore(FileSystemBlobStore):
    def head(self, ref: ObjectRef):
        return replace(super().head(ref), verified=False)


def test_repository_publishes_one_canonical_blob_and_round_trips(tmp_path) -> None:
    blobs = FileSystemBlobStore(tmp_path / "cas")
    catalog = _MemoryCatalog()
    repository = DurableDatasetSnapshotRepository(blobs, catalog)
    snapshot = _snapshot()

    assert repository.publish(snapshot, idempotency_key="dataset:one") == snapshot.ref
    assert repository.get(snapshot.ref) == snapshot
    assert catalog.publish_calls == 1

    objects = tuple((tmp_path / "cas" / "sha256").glob("*/*"))
    assert len(objects) == 1
    assert objects[0].read_bytes() == encode_dataset_snapshot(snapshot)


def test_reader_rejects_catalog_projection_disagreement(tmp_path) -> None:
    blobs = FileSystemBlobStore(tmp_path / "cas")
    catalog = _MemoryCatalog()
    repository = DurableDatasetSnapshotRepository(blobs, catalog)
    snapshot = _snapshot()
    repository.publish(snapshot, idempotency_key="dataset:projection")
    assert catalog.entry is not None
    projection = catalog.entry.projection
    catalog.entry = replace(
        catalog.entry,
        projection=replace(projection, evaluated_method_id="substituted-method"),
    )

    with pytest.raises(DatasetSnapshotIntegrityError, match="projection"):
        repository.get(snapshot.ref)


def test_reader_requires_verified_exact_blob_metadata(tmp_path) -> None:
    blobs = _UnverifiedBlobStore(tmp_path / "cas")
    catalog = _MemoryCatalog()
    repository = DurableDatasetSnapshotRepository(blobs, catalog)
    snapshot = _snapshot()
    repository.publish(snapshot, idempotency_key="dataset:metadata")

    with pytest.raises(DatasetSnapshotIntegrityError, match="metadata"):
        repository.get(snapshot.ref)


def test_repository_retry_does_not_create_per_member_objects(tmp_path) -> None:
    blobs = FileSystemBlobStore(tmp_path / "cas")
    catalog = _MemoryCatalog()
    repository = DurableDatasetSnapshotRepository(blobs, catalog)
    snapshot = _snapshot()

    first = repository.publish(snapshot, idempotency_key="dataset:retry")
    second = repository.publish(snapshot, idempotency_key="dataset:retry")

    assert first == second == snapshot.ref
    assert len(tuple((tmp_path / "cas" / "sha256").glob("*/*"))) == 1
