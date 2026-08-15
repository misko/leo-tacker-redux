"""SQLite-backed immutable capture plans, not a scheduling queue."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from leo_flow.contracts.capture import CapturePlan, CapturePlanRef
from leo_flow.contracts.core import PlanId, canonical_digest

from .serialization import decode_plan, encode_plan


class CapturePlanConflictError(RuntimeError):
    """A plan identity, key, or stored digest was reused inconsistently."""


class SQLiteCapturePlanRepository:
    """One durable implementation of both public capture-plan ports."""

    def __init__(
        self,
        database_path: Path,
        *,
        now_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_ns = now_ns
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
                CREATE TABLE IF NOT EXISTS capture_plans (
                    plan_id TEXT PRIMARY KEY,
                    plan_digest TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload BLOB NOT NULL,
                    accepted_utc_ns INTEGER NOT NULL
                )
                """
            )

    def publish(self, plan: CapturePlan, *, idempotency_key: str) -> CapturePlanRef:
        if not idempotency_key:
            raise ValueError("idempotency key cannot be empty")
        payload = encode_plan(plan)
        digest = canonical_digest(plan)
        expected = (str(plan.plan_id), str(digest), idempotency_key, payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT plan_id, plan_digest, idempotency_key, payload
                  FROM capture_plans
                 WHERE plan_id = ? OR idempotency_key = ?
                """,
                (str(plan.plan_id), idempotency_key),
            ).fetchall()
            if rows:
                if len(rows) != 1 or tuple(rows[0]) != expected:
                    raise CapturePlanConflictError(
                        "plan ID or idempotency key identifies another plan"
                    )
                return CapturePlanRef(plan.plan_id, digest)
            connection.execute(
                """
                INSERT INTO capture_plans(
                    plan_id, plan_digest, idempotency_key, payload, accepted_utc_ns
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (*expected, self._now_ns()),
            )
        return CapturePlanRef(plan.plan_id, digest)

    def get(self, plan_id: PlanId) -> CapturePlan:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT plan_digest, payload FROM capture_plans WHERE plan_id = ?
                """,
                (str(plan_id),),
            ).fetchone()
        if row is None:
            raise KeyError(plan_id)
        payload = bytes(row["payload"])
        plan = decode_plan(payload)
        if (
            plan.plan_id != plan_id
            or str(canonical_digest(plan)) != row["plan_digest"]
            or encode_plan(plan) != payload
        ):
            raise CapturePlanConflictError(
                "durable capture plan failed integrity check"
            )
        return plan
