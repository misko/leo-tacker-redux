from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from leo_flow.capture.plan_repository import (
    CapturePlanConflictError,
    SQLiteCapturePlanRepository,
)
from leo_flow.contracts.core import PlanId, canonical_digest

from ._helpers import plan_with_activities


def test_plan_publication_round_trips_exactly_across_restart(tmp_path) -> None:
    database = tmp_path / "capture.sqlite3"
    plan = plan_with_activities()
    first = SQLiteCapturePlanRepository(database, now_ns=lambda: 100)
    ref = first.publish(plan, idempotency_key="dwell:stable")

    restarted = SQLiteCapturePlanRepository(database, now_ns=lambda: 200)
    assert restarted.publish(plan, idempotency_key="dwell:stable") == ref
    assert restarted.get(plan.plan_id) == plan
    assert ref.plan_digest == canonical_digest(plan)


def test_plan_id_and_idempotency_key_conflicts_fail_closed(tmp_path) -> None:
    repository = SQLiteCapturePlanRepository(tmp_path / "capture.sqlite3")
    plan = plan_with_activities()
    repository.publish(plan, idempotency_key="dwell:stable")

    with pytest.raises(CapturePlanConflictError, match="another plan"):
        repository.publish(
            replace(plan, experiment_tags=(("changed", True),)),
            idempotency_key="dwell:stable",
        )
    with pytest.raises(CapturePlanConflictError, match="another plan"):
        repository.publish(
            replace(plan, plan_id=PlanId("plan_other")),
            idempotency_key="dwell:stable",
        )
    with pytest.raises(CapturePlanConflictError, match="another plan"):
        repository.publish(plan, idempotency_key="dwell:other")


def test_plan_read_detects_corrupted_digest(tmp_path) -> None:
    database = tmp_path / "capture.sqlite3"
    repository = SQLiteCapturePlanRepository(database)
    plan = plan_with_activities()
    repository.publish(plan, idempotency_key="dwell:stable")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE capture_plans SET plan_digest = ? WHERE plan_id = ?",
            ("sha256:" + "0" * 64, str(plan.plan_id)),
        )
    with pytest.raises(CapturePlanConflictError, match="integrity"):
        repository.get(plan.plan_id)


def test_plan_read_rejects_noncanonical_extra_payload_fields(tmp_path) -> None:
    database = tmp_path / "capture.sqlite3"
    repository = SQLiteCapturePlanRepository(database)
    plan = plan_with_activities()
    repository.publish(plan, idempotency_key="dwell:stable")
    with sqlite3.connect(database) as connection:
        payload = connection.execute(
            "SELECT payload FROM capture_plans WHERE plan_id = ?",
            (str(plan.plan_id),),
        ).fetchone()[0]
        connection.execute(
            "UPDATE capture_plans SET payload = ? WHERE plan_id = ?",
            (payload[:-1] + b',"unknown":true}', str(plan.plan_id)),
        )
    with pytest.raises(CapturePlanConflictError, match="integrity"):
        repository.get(plan.plan_id)
