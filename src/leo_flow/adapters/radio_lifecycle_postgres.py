"""Append-only PostgreSQL lifecycle facts and bounded dashboard reads."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

import psycopg
from psycopg.types.json import Jsonb

from leo_flow.capture.radio_lifecycle import lifecycle_dashboard_view
from leo_flow.capture.radio_lifecycle_codec import (
    decode_attempt_lifecycle_fact,
    encode_attempt_lifecycle_fact,
)
from leo_flow.contracts.core import (
    V0_1,
    CaptureAttemptId,
    RadioId,
    SchemaRef,
    canonical_json_bytes,
)
from leo_flow.contracts.radio_lifecycle import (
    CaptureAttemptLifecycleDashboardViewV0_1,
    CaptureAttemptLifecycleFactV0_1,
    RadioLifecycleIntervalFactV0_1,
    RadioLifecycleObservationV0_1,
)
from leo_flow.dashboard import DashboardNotFound

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresRadioLifecycleRepositoryV0_1:
    """Implements fact recorder, history, and dashboard query narrow ports."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def record_attempt(
        self, fact: CaptureAttemptLifecycleFactV0_1
    ) -> CaptureAttemptLifecycleFactV0_1:
        encoded = encode_attempt_lifecycle_fact(fact)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT public.publish_capture_attempt_radio_lifecycle_fact("
                "%(fact)s::jsonb,%(sha)s) AS attempt_id",
                {
                    "fact": Jsonb(json.loads(encoded)),
                    "sha": hashlib.sha256(encoded).hexdigest(),
                },
            ).fetchone()
        if row is None or str(row["attempt_id"]) != str(fact.attempt_id):
            raise RuntimeError("lifecycle attempt fact returned an invalid receipt")
        return fact

    def record_interval(
        self, fact: RadioLifecycleIntervalFactV0_1
    ) -> RadioLifecycleIntervalFactV0_1:
        encoded = canonical_json_bytes(fact)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT public.publish_radio_lifecycle_interval_fact("
                "%(fact)s::jsonb,%(sha)s) AS current_attempt_id",
                {
                    "fact": Jsonb(json.loads(encoded)),
                    "sha": hashlib.sha256(encoded).hexdigest(),
                },
            ).fetchone()
        if row is None or str(row["current_attempt_id"]) != str(
            fact.current_attempt_id
        ):
            raise RuntimeError("lifecycle interval fact returned an invalid receipt")
        return fact

    def latest_terminal(
        self, radio_id: RadioId
    ) -> tuple[CaptureAttemptId, RadioLifecycleObservationV0_1] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT public.read_latest_radio_lifecycle_terminal(%(radio_id)s) "
                "AS semantic_fact",
                {"radio_id": str(radio_id)},
            ).fetchone()
        if row is None or row["semantic_fact"] is None:
            return None
        fact = decode_attempt_lifecycle_fact(row["semantic_fact"])
        if fact.radio_id != radio_id:
            raise RuntimeError("lifecycle history returned another radio")
        return fact.attempt_id, fact.terminal

    def capture_attempt_radio_lifecycle(
        self, attempt_id: CaptureAttemptId
    ) -> CaptureAttemptLifecycleDashboardViewV0_1:
        with self._connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            row = connection.execute(
                "SELECT public.read_capture_attempt_radio_lifecycle_fact("
                "%(attempt_id)s) AS semantic_fact",
                {"attempt_id": str(attempt_id)},
            ).fetchone()
        if row is None or row["semantic_fact"] is None:
            raise DashboardNotFound(
                f"radio lifecycle for capture attempt {attempt_id} was not found"
            )
        fact = decode_attempt_lifecycle_fact(row["semantic_fact"])
        if fact.attempt_id != attempt_id:
            raise RuntimeError("lifecycle query returned another capture attempt")
        return lifecycle_dashboard_view(
            fact,
            schema=SchemaRef(CaptureAttemptLifecycleDashboardViewV0_1.SCHEMA_ID, V0_1),
        )
