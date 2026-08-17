"""Strict private SQLite adapter for capture-batch optimistic state."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from leo_flow.application.capture_batches import (
    CaptureBatchIdentityConflict,
    CaptureBatchNotFound,
    CaptureBatchRevisionConflict,
)
from leo_flow.capture.batch_serialization import (
    decode_batch_definition,
    decode_batch_snapshot,
    encode_batch_definition,
    encode_batch_snapshot,
)
from leo_flow.contracts.capture_batch import CaptureBatchSnapshot
from leo_flow.contracts.core import CaptureBatchId, canonical_digest


class SQLiteCaptureBatchStateStore:
    """One-row-per-batch CAS store containing only canonical public contracts."""

    def __init__(
        self, database_path: Path, *, now_utc_ns: Callable[[], int] = time.time_ns
    ) -> None:
        if not database_path.is_absolute() or ".." in database_path.parts:
            raise ValueError(
                "capture batch database path must be absolute and normalized"
            )
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._now = now_utc_ns
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS capture_batch_state (
                    batch_id TEXT PRIMARY KEY,
                    definition_digest TEXT NOT NULL,
                    definition_payload BLOB NOT NULL,
                    snapshot_payload BLOB NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision BETWEEN 0 AND 2),
                    updated_utc_ns INTEGER NOT NULL
                )
                """
            )

    def create(self, initial: CaptureBatchSnapshot) -> CaptureBatchSnapshot:
        definition = encode_batch_definition(initial.definition)
        snapshot = encode_batch_snapshot(initial)
        digest = str(canonical_digest(initial.definition))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM capture_batch_state WHERE batch_id = ?",
                (str(initial.batch_id),),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO capture_batch_state(
                        batch_id, definition_digest, definition_payload,
                        snapshot_payload, revision, updated_utc_ns
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(initial.batch_id),
                        digest,
                        definition,
                        snapshot,
                        initial.revision,
                        self._now(),
                    ),
                )
                return initial
            current = self._decode_row(row)
            if current.definition != initial.definition:
                raise CaptureBatchIdentityConflict(
                    "batch ID already identifies another definition"
                )
            return current

    def get(self, batch_id: CaptureBatchId) -> CaptureBatchSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM capture_batch_state WHERE batch_id = ?",
                (str(batch_id),),
            ).fetchone()
        return None if row is None else self._decode_row(row)

    def compare_and_swap(
        self,
        batch_id: CaptureBatchId,
        expected_revision: int,
        replacement: CaptureBatchSnapshot,
    ) -> CaptureBatchSnapshot:
        if replacement.batch_id != batch_id or replacement.revision != (
            expected_revision + 1
        ):
            raise CaptureBatchIdentityConflict(
                "replacement identity or revision differs"
            )
        payload = encode_batch_snapshot(replacement)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM capture_batch_state WHERE batch_id = ?",
                (str(batch_id),),
            ).fetchone()
            if row is None:
                raise CaptureBatchNotFound(f"capture batch {batch_id} was not found")
            current = self._decode_row(row)
            if current.revision != expected_revision:
                raise CaptureBatchRevisionConflict("capture batch revision changed")
            if current.definition != replacement.definition:
                raise CaptureBatchIdentityConflict(
                    "replacement changed immutable batch definition"
                )
            cursor = connection.execute(
                """
                UPDATE capture_batch_state
                   SET snapshot_payload = ?, revision = ?, updated_utc_ns = ?
                 WHERE batch_id = ? AND revision = ?
                """,
                (
                    payload,
                    replacement.revision,
                    self._now(),
                    str(batch_id),
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise CaptureBatchRevisionConflict("capture batch revision changed")
        return replacement

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> CaptureBatchSnapshot:
        try:
            definition_payload = bytes(row["definition_payload"])
            snapshot_payload = bytes(row["snapshot_payload"])
            definition = decode_batch_definition(definition_payload)
            snapshot = decode_batch_snapshot(snapshot_payload)
            revision = row["revision"]
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or snapshot.batch_id != CaptureBatchId(row["batch_id"])
                or snapshot.definition != definition
                or snapshot.revision != revision
                or str(canonical_digest(definition)) != row["definition_digest"]
            ):
                raise ValueError
            return snapshot
        except (KeyError, TypeError, ValueError) as error:
            raise CaptureBatchIdentityConflict(
                "durable capture batch failed integrity validation"
            ) from error
