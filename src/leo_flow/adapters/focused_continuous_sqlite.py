"""Durable journal for the continuous focused capture/analysis supervisor."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class FocusedContinuousRecordV0_1:
    sequence: int
    monitor_id: str
    requested_start_utc_ns: int
    definition_digest: str
    state_root: Path
    batch_id: str
    state: str
    error: str | None = None


class SQLiteFocusedContinuousJournalV0_1:
    """Full-sync exact-transition journal; capture plans are never replayed."""

    _STATES: ClassVar[set[str]] = {
        "planned",
        "captured",
        "analysis_running",
        "complete",
        "failed",
    }

    def __init__(self, path: Path) -> None:
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError(
                "focused continuous journal path must be canonical absolute"
            )
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._path = path
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS focused_dwell (
                    sequence INTEGER PRIMARY KEY CHECK (sequence >= 0),
                    monitor_id TEXT NOT NULL UNIQUE,
                    requested_start_utc_ns INTEGER NOT NULL CHECK (
                        requested_start_utc_ns >= 0),
                    definition_digest TEXT NOT NULL CHECK (
                        definition_digest GLOB 'sha256:[0-9a-f]*' AND
                        length(definition_digest) = 71),
                    state_root TEXT NOT NULL UNIQUE,
                    batch_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (state IN (
                        'planned','captured','analysis_running','complete','failed')),
                    error TEXT,
                    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)
                ) STRICT
                """
            )

    def next_sequence(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT coalesce(max(sequence),-1)+1 FROM focused_dwell"
            ).fetchone()
        assert row is not None
        return int(row[0])

    def insert_planned(self, record: FocusedContinuousRecordV0_1) -> None:
        if record.state != "planned" or record.error is not None:
            raise ValueError("new focused record must be a clean planned record")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO focused_dwell(
                    sequence,monitor_id,requested_start_utc_ns,
                    definition_digest,state_root,batch_id,state,error)
                VALUES (?,?,?,?,?,?,?,NULL)
                """,
                (
                    record.sequence,
                    record.monitor_id,
                    record.requested_start_utc_ns,
                    record.definition_digest,
                    str(record.state_root),
                    record.batch_id,
                    record.state,
                ),
            )

    def transition(
        self,
        sequence: int,
        expected: str,
        target: str,
        *,
        error: str | None = None,
    ) -> None:
        if expected not in self._STATES or target not in self._STATES:
            raise ValueError("unknown focused journal state")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE focused_dwell
                   SET state=?,error=?,revision=revision+1
                 WHERE sequence=? AND state=?
                """,
                (target, error, sequence, expected),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("focused journal transition conflict")

    def incomplete(self) -> tuple[FocusedContinuousRecordV0_1, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence,monitor_id,requested_start_utc_ns,
                       definition_digest,state_root,batch_id,state,error
                  FROM focused_dwell
                 WHERE state NOT IN ('complete','failed')
                 ORDER BY sequence
                """
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def get(self, sequence: int) -> FocusedContinuousRecordV0_1 | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT sequence,monitor_id,requested_start_utc_ns,
                       definition_digest,state_root,batch_id,state,error
                  FROM focused_dwell WHERE sequence=?
                """,
                (sequence,),
            ).fetchone()
        return None if row is None else self._record(row)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, isolation_level="IMMEDIATE")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _record(row: tuple[object, ...]) -> FocusedContinuousRecordV0_1:
        return FocusedContinuousRecordV0_1(
            int(str(row[0])),
            str(row[1]),
            int(str(row[2])),
            str(row[3]),
            Path(str(row[4])),
            str(row[5]),
            str(row[6]),
            None if row[7] is None else str(row[7]),
        )
