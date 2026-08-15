"""Private SQLite durability for capture and publication recovery."""

from __future__ import annotations

import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from leo_flow.contracts.capture import CompletedLocalRecording
from leo_flow.contracts.core import PlanId, RecordingId
from leo_flow.contracts.storage import PublishedRecordingRef

from .serialization import decode_completed, encode_completed

_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class SpoolState(StrEnum):
    ALLOCATED = "allocated"
    COMPLETE = "complete"
    FAILED = "failed"
    ACKNOWLEDGED = "acknowledged"
    CLEANED = "cleaned"


@dataclass(frozen=True)
class SpoolEntry:
    recording_id: RecordingId
    plan_id: PlanId
    destination: str
    state: SpoolState
    recording: CompletedLocalRecording | None
    publish_attempts: int
    last_error: str | None
    idempotency_key: str | None


class SQLiteLocalSpool:
    """Single-host capture state; it is not a scientific catalog."""

    def __init__(
        self,
        database_path: Path,
        recording_root: Path,
        *,
        id_factory: Callable[[], RecordingId] | None = None,
        now_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.database_path = Path(database_path)
        self.recording_root = Path(recording_root)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.recording_root.mkdir(parents=True, exist_ok=True)
        self._id_factory = id_factory or _new_recording_id
        self._now_ns = now_ns
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recordings (
                    recording_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    destination TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (
                        state IN ('allocated','complete','failed','acknowledged','cleaned')
                    ),
                    payload BLOB,
                    publish_attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    idempotency_key TEXT,
                    published_identity_digest TEXT,
                    created_utc_ns INTEGER NOT NULL,
                    updated_utc_ns INTEGER NOT NULL
                )
                """
            )

    def allocate(self, plan_id: PlanId) -> tuple[RecordingId, str]:
        for _ in range(8):
            recording_id = self._id_factory()
            destination = str(self.recording_root / str(recording_id))
            now = self._now_ns()
            try:
                with self._connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO recordings(
                            recording_id, plan_id, destination, state,
                            created_utc_ns, updated_utc_ns
                        ) VALUES (?, ?, ?, 'allocated', ?, ?)
                        """,
                        (str(recording_id), str(plan_id), destination, now, now),
                    )
                return recording_id, destination
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("could not allocate a unique recording ID")

    def record_complete(self, recording: CompletedLocalRecording) -> None:
        payload = encode_completed(recording)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state, payload, plan_id FROM recordings WHERE recording_id = ?",
                (str(recording.recording_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown spool recording {recording.recording_id}")
            if row["state"] == SpoolState.COMPLETE and row["payload"] == payload:
                return
            if row["state"] != SpoolState.ALLOCATED:
                raise RuntimeError(f"cannot complete recording in state {row['state']}")
            if row["plan_id"] != str(recording.manifest.plan_id):
                raise RuntimeError("completed recording belongs to a different plan")
            connection.execute(
                """
                UPDATE recordings
                   SET state = 'complete', payload = ?, last_error = NULL,
                       updated_utc_ns = ?
                 WHERE recording_id = ? AND state = 'allocated'
                """,
                (payload, self._now_ns(), str(recording.recording_id)),
            )

    def record_failure(self, recording_id: RecordingId, reason: str) -> None:
        if not reason:
            raise ValueError("failure reason cannot be empty")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE recordings
                   SET state = 'failed', last_error = ?, updated_utc_ns = ?
                 WHERE recording_id = ? AND state = 'allocated'
                """,
                (reason, self._now_ns(), str(recording_id)),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT state FROM recordings WHERE recording_id = ?",
                    (str(recording_id),),
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown spool recording {recording_id}")
                if row["state"] != SpoolState.FAILED:
                    raise RuntimeError(f"cannot fail recording in state {row['state']}")

    def fail_incomplete_allocations(
        self, reason: str = "capture process restarted"
    ) -> int:
        """Fail every allocation without inspecting local objects.

        Production restart code should first call ``incomplete_allocations``
        and attempt codec-owned recovery. This bulk transition remains useful
        for explicit administrative abandonment and compatibility tests.
        """
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE recordings
                   SET state = 'failed', last_error = ?, updated_utc_ns = ?
                 WHERE state = 'allocated'
                """,
                (reason, self._now_ns()),
            )
            return cursor.rowcount

    def incomplete_allocations(self, limit: int = 100) -> tuple[SpoolEntry, ...]:
        """Return bounded recovery work without interpreting storage paths."""

        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM recordings
                 WHERE state = 'allocated'
                 ORDER BY created_utc_ns, recording_id
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(_entry_from_row(row) for row in rows)

    def pending_publication(
        self, limit: int = 100
    ) -> tuple[CompletedLocalRecording, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM recordings
                 WHERE state = 'complete'
                 ORDER BY created_utc_ns, recording_id
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(decode_completed(row["payload"]) for row in rows)

    def pending_cleanup(self, limit: int = 100) -> tuple[CompletedLocalRecording, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM recordings
                 WHERE state = 'acknowledged'
                 ORDER BY updated_utc_ns, recording_id
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(decode_completed(row["payload"]) for row in rows)

    def has_durable_recording(self, plan_id: PlanId) -> bool:
        """Return whether a plan already produced bytes that must not be recaptured."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM recordings
                 WHERE plan_id = ?
                   AND state IN ('complete', 'acknowledged', 'cleaned')
                 LIMIT 1
                """,
                (str(plan_id),),
            ).fetchone()
        return row is not None

    def durable_recording_for_plan(self, plan_id: PlanId) -> SpoolEntry | None:
        """Return the single durable recording receipt for an idempotent plan."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM recordings
                 WHERE plan_id = ?
                   AND state IN ('complete', 'acknowledged', 'cleaned')
                 ORDER BY created_utc_ns, recording_id
                 LIMIT 2
                """,
                (str(plan_id),),
            ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("plan has more than one durable recording")
        return _entry_from_row(rows[0]) if rows else None

    def durable_recordings(self, limit: int) -> tuple[SpoolEntry, ...]:
        """Return bounded retained-byte receipts for capacity policy."""

        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM recordings
                 WHERE state IN ('complete', 'acknowledged', 'cleaned')
                 ORDER BY created_utc_ns, recording_id
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(_entry_from_row(row) for row in rows)

    def note_publish_attempt(
        self, recording_id: RecordingId, idempotency_key: str, error: str | None
    ) -> None:
        if not idempotency_key:
            raise ValueError("idempotency key cannot be empty")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE recordings
                   SET publish_attempts = publish_attempts + 1,
                       idempotency_key = ?, last_error = ?, updated_utc_ns = ?
                 WHERE recording_id = ? AND state = 'complete'
                """,
                (idempotency_key, error, self._now_ns(), str(recording_id)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("publish attempt requires a complete recording")

    def acknowledge(
        self,
        recording: CompletedLocalRecording,
        published: PublishedRecordingRef,
        idempotency_key: str,
    ) -> None:
        if published.recording_id != recording.recording_id:
            raise ValueError("publisher acknowledged a different recording")
        identity = str(published.recording_object.identity_digest())
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT state, published_identity_digest, idempotency_key
                  FROM recordings WHERE recording_id = ?
                """,
                (str(recording.recording_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown spool recording {recording.recording_id}")
            if row["state"] in (SpoolState.ACKNOWLEDGED, SpoolState.CLEANED):
                if (
                    row["published_identity_digest"] != identity
                    or row["idempotency_key"] != idempotency_key
                ):
                    raise RuntimeError("publication acknowledgement collision")
                return
            if row["state"] != SpoolState.COMPLETE:
                raise RuntimeError(
                    f"cannot acknowledge recording in state {row['state']}"
                )
            connection.execute(
                """
                UPDATE recordings
                   SET state = 'acknowledged', published_identity_digest = ?,
                       idempotency_key = ?, last_error = NULL, updated_utc_ns = ?
                 WHERE recording_id = ? AND state = 'complete'
                """,
                (
                    identity,
                    idempotency_key,
                    self._now_ns(),
                    str(recording.recording_id),
                ),
            )

    def mark_cleaned(self, recording_id: RecordingId) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE recordings SET state = 'cleaned', updated_utc_ns = ?
                 WHERE recording_id = ? AND state = 'acknowledged'
                """,
                (self._now_ns(), str(recording_id)),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT state FROM recordings WHERE recording_id = ?",
                    (str(recording_id),),
                ).fetchone()
                if row is None or row["state"] != SpoolState.CLEANED:
                    raise RuntimeError("cleanup completion requires acknowledgement")

    def get(self, recording_id: RecordingId) -> SpoolEntry:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recordings WHERE recording_id = ?",
                (str(recording_id),),
            ).fetchone()
        if row is None:
            raise KeyError(recording_id)
        return _entry_from_row(row)


def _entry_from_row(row: sqlite3.Row) -> SpoolEntry:
    payload = row["payload"]
    return SpoolEntry(
        recording_id=RecordingId(row["recording_id"]),
        plan_id=PlanId(row["plan_id"]),
        destination=row["destination"],
        state=SpoolState(row["state"]),
        recording=decode_completed(payload) if payload is not None else None,
        publish_attempts=row["publish_attempts"],
        last_error=row["last_error"],
        idempotency_key=row["idempotency_key"],
    )


def _new_recording_id() -> RecordingId:
    """Generate a time-sortable ULID without adding a capture dependency."""
    value = (time.time_ns() // 1_000_000) << 80 | secrets.randbits(80)
    encoded = "".join(
        _CROCKFORD32[(value >> shift) & 31] for shift in range(125, -1, -5)
    )
    return RecordingId(f"rec_{encoded}")
