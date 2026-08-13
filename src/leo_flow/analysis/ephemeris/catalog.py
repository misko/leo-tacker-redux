"""Narrow catalog port and semantic in-memory implementation."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import EphemerisRetrievalId, EphemerisSnapshotId
from leo_flow.contracts.ephemeris import (
    EphemerisSnapshot,
    EphemerisSnapshotRef,
    EphemerisSource,
)
from leo_flow.contracts.storage import ObjectRef

from .resolver import SnapshotRecord


@dataclass(frozen=True)
class ArchivedEphemerisSnapshot:
    snapshot: EphemerisSnapshot
    provenance_object_ref: ObjectRef
    request_spec_digest: str

    def snapshot_ref(self) -> EphemerisSnapshotRef:
        return EphemerisSnapshotRef(
            self.snapshot.snapshot_id,
            self.snapshot.source,
            self.snapshot.raw_object_ref.digest,
            self.snapshot.normalized_object_ref.digest,
        )


class EphemerisCatalogConflictError(RuntimeError):
    pass


class EphemerisSnapshotCatalog(Protocol):
    def publish(self, archived: ArchivedEphemerisSnapshot) -> None: ...

    def get_by_retrieval(
        self, retrieval_id: EphemerisRetrievalId
    ) -> ArchivedEphemerisSnapshot | None: ...

    def get(
        self, snapshot_id: EphemerisSnapshotId
    ) -> ArchivedEphemerisSnapshot | None: ...

    def history(self, source: EphemerisSource, scope: str) -> tuple[SnapshotRecord, ...]: ...


class InMemoryEphemerisSnapshotCatalog:
    """Thread-safe semantic fake with the publication invariants a DB must keep."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshots: dict[EphemerisSnapshotId, ArchivedEphemerisSnapshot] = {}
        self._retrievals: dict[
            EphemerisRetrievalId, ArchivedEphemerisSnapshot
        ] = {}

    def publish(self, archived: ArchivedEphemerisSnapshot) -> None:
        snapshot = archived.snapshot
        with self._lock:
            by_id = self._snapshots.get(snapshot.snapshot_id)
            by_retrieval = self._retrievals.get(snapshot.retrieval_id)
            for prior in (by_id, by_retrieval):
                if prior is not None and prior != archived:
                    raise EphemerisCatalogConflictError(
                        "snapshot or retrieval ID identifies different content"
                    )
            self._snapshots[snapshot.snapshot_id] = archived
            self._retrievals[snapshot.retrieval_id] = archived

    def get_by_retrieval(
        self, retrieval_id: EphemerisRetrievalId
    ) -> ArchivedEphemerisSnapshot | None:
        with self._lock:
            return self._retrievals.get(retrieval_id)

    def get(
        self, snapshot_id: EphemerisSnapshotId
    ) -> ArchivedEphemerisSnapshot | None:
        with self._lock:
            return self._snapshots.get(snapshot_id)

    def history(
        self, source: EphemerisSource, scope: str
    ) -> tuple[SnapshotRecord, ...]:
        with self._lock:
            matches = [
                SnapshotRecord(
                    item.snapshot_ref(), item.snapshot.retrieved_at_utc_ns
                )
                for item in self._snapshots.values()
                if item.snapshot.source is source and item.snapshot.scope == scope
            ]
        return tuple(
            sorted(
                matches,
                key=lambda item: (
                    int(item.retrieval_completed_utc_ns),
                    str(item.snapshot_ref.snapshot_id),
                ),
            )
        )
