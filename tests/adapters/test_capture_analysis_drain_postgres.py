from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Self

import pytest

from leo_flow.adapters import capture_analysis_drain_postgres
from leo_flow.adapters.capture_analysis_drain_postgres import (
    PostgresCaptureAnalysisDrainGate,
)


class _Result:
    def __init__(self, row: tuple[object, ...] | None = None) -> None:
        self._row = row

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _Connection:
    def __init__(self, rows: Sequence[tuple[object, ...] | None]) -> None:
        self._rows = iter(rows)
        self.calls: list[tuple[str, tuple[object, ...] | None]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(
        self, statement: str, parameters: tuple[object, ...] | None = None
    ) -> _Result:
        normalized = " ".join(statement.split())
        self.calls.append((normalized, parameters))
        if normalized.startswith("SELECT"):
            return _Result(next(self._rows))
        return _Result()


def _install_connection(
    monkeypatch: pytest.MonkeyPatch,
    connection: _Connection,
) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def connect(dsn: str, **kwargs: Any) -> _Connection:
        calls.append((dsn, kwargs))
        return connection

    monkeypatch.setattr(capture_analysis_drain_postgres.psycopg, "connect", connect)
    return calls


def test_gate_proves_membership_assumes_capture_role_and_uses_bounded_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(((True,), (True,)))
    connect_calls = _install_connection(monkeypatch, connection)

    assert PostgresCaptureAnalysisDrainGate("postgresql://private").ready()

    assert connect_calls == [
        (
            "postgresql://private",
            {
                "connect_timeout": 5,
                "options": "-c statement_timeout=5000 -c lock_timeout=5000",
            },
        )
    ]
    assert connection.calls == [
        ("SET TRANSACTION READ ONLY", None),
        (
            "SELECT pg_has_role(current_user, %s, 'MEMBER') AS member",
            ("leo_capture",),
        ),
        ("SET ROLE leo_capture", None),
        ("SELECT public.capture_analysis_drain_ready() AS ready", None),
    ]


@pytest.mark.parametrize("membership", [False, None, "true"])
def test_gate_rejects_nonmember_or_malformed_membership_before_assuming_role(
    monkeypatch: pytest.MonkeyPatch,
    membership: object,
) -> None:
    row = None if membership is None else (membership,)
    connection = _Connection((row,))
    _install_connection(monkeypatch, connection)

    with pytest.raises(RuntimeError, match="not a leo_capture role member"):
        PostgresCaptureAnalysisDrainGate("postgresql://private").ready()

    assert connection.calls == [
        ("SET TRANSACTION READ ONLY", None),
        (
            "SELECT pg_has_role(current_user, %s, 'MEMBER') AS member",
            ("leo_capture",),
        ),
    ]


@pytest.mark.parametrize("decision", [None, 1, "false"])
def test_gate_rejects_missing_or_non_boolean_decision(
    monkeypatch: pytest.MonkeyPatch,
    decision: object,
) -> None:
    row = None if decision is None else (decision,)
    connection = _Connection(((True,), row))
    _install_connection(monkeypatch, connection)

    with pytest.raises(RuntimeError, match="returned no decision"):
        PostgresCaptureAnalysisDrainGate("postgresql://private").ready()
