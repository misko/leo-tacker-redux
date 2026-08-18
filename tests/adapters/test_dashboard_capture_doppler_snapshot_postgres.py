from __future__ import annotations

from typing import Any

import pytest

from leo_flow.adapters.dashboard_capture_doppler_postgres import (
    PostgresCaptureDopplerSnapshotRepositoryV0_1,
)
from leo_flow.contracts.core import RecordingId, UtcNs
from leo_flow.contracts.dashboard_master_capture import (
    MasterCaptureSnapshotQueryV0_1,
)


class _Cursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.statements: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: str, parameters: object = None):
        self.statements.append((statement, parameters))
        return self

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self, **kwargs: Any):
        del kwargs
        return self._cursor


def test_master_doppler_adapter_is_one_bounded_call_with_five_explicit_states(
) -> None:
    rows = [
        _row("rec_complete", "complete", candidate=1, assignments=2),
        _row("rec_complete", "complete", candidate=2, assignments=2),
        _row("rec_pending", "pending"),
        _row("rec_no_candidate", "no_candidate"),
        _row("rec_not_analyzed", "not_analyzed"),
        _row("rec_failed", "failed"),
    ]
    cursor = _Cursor(rows)
    repository = PostgresCaptureDopplerSnapshotRepositoryV0_1(
        lambda: _Connection(cursor)  # type: ignore[arg-type,return-value]
    )
    requested = tuple(
        RecordingId(value)
        for value in (
            "rec_complete",
            "rec_pending",
            "rec_no_candidate",
            "rec_not_analyzed",
            "rec_failed",
        )
    )

    result = repository.capture_doppler_snapshot(
        MasterCaptureSnapshotQueryV0_1(UtcNs(1), UtcNs(10), 5), requested
    )

    assert [result[item].state.value for item in requested] == [
        "complete",
        "pending",
        "no_candidate",
        "not_analyzed",
        "failed",
    ]
    candidates = result[RecordingId("rec_complete")].candidates
    assert [
        (item.radio_id, item.lnb_id, item.receiver_chain_id)
        for item in candidates
    ] == [
        ("radio_a", "lnb_1", "rx_1"),
        ("radio_a", "lnb_2", "rx_2"),
    ]
    assert len(cursor.statements) == 2
    assert cursor.statements[0][0] == "SET TRANSACTION READ ONLY"
    routine_sql, parameters = cursor.statements[1]
    assert routine_sql.count("read_dashboard_capture_doppler_summaries_v0_1") == 1
    assert "ANY(%s::text[])" in routine_sql
    assert "object_blob" not in routine_sql
    assert "locator" not in routine_sql
    assert parameters == (1, 10, 5, [str(item) for item in requested])


def test_master_doppler_adapter_rejects_rows_outside_requested_closure() -> None:
    cursor = _Cursor([_row("rec_other", "not_analyzed")])
    repository = PostgresCaptureDopplerSnapshotRepositoryV0_1(
        lambda: _Connection(cursor)  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(RuntimeError, match="recording closure"):
        repository.capture_doppler_snapshot(
            MasterCaptureSnapshotQueryV0_1(UtcNs(1), UtcNs(10), 1),
            (RecordingId("rec_requested"),),
        )


def _row(
    recording_id: str,
    state: str,
    *,
    candidate: int | None = None,
    assignments: int = 1,
) -> dict[str, object]:
    has_candidate = candidate is not None
    return {
        "recording_id": recording_id,
        "radio_id": "radio_a",
        "analysis_state": "complete",
        "summary_state": state,
        "assignment_count": assignments,
        "lnb_id": f"lnb_{candidate}" if has_candidate else None,
        "receiver_chain_id": f"rx_{candidate}" if has_candidate else None,
        "segment_id": f"seg_{candidate}" if has_candidate else None,
        "candidate_id": f"candidate_{candidate}" if has_candidate else None,
        "model": "linear" if has_candidate else None,
        "drift_rate_hz_s": float(candidate or 0) if has_candidate else None,
        "ranking_score": 1.0 / int(candidate or 1) if has_candidate else None,
        "doppler_id": f"doppler_{candidate}" if has_candidate else None,
        "algorithm_version": "blind-v0.1" if has_candidate else None,
        "original_recording_count": 5,
    }
