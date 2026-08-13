"""Narrow persistence ports for durable dataset snapshots."""

from __future__ import annotations

from typing import Protocol

from .snapshot import DatasetSnapshotBundle, DatasetSnapshotRef


class DatasetSnapshotReader(Protocol):
    def get(self, ref: DatasetSnapshotRef) -> DatasetSnapshotBundle: ...


class DatasetSnapshotPublisher(Protocol):
    def publish(
        self, snapshot: DatasetSnapshotBundle, *, idempotency_key: str
    ) -> DatasetSnapshotRef: ...
