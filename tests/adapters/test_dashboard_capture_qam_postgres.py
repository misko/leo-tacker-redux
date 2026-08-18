from __future__ import annotations

from typing import Any

import pytest

from leo_flow.adapters.dashboard_capture_qam_postgres import (
    PostgresCaptureQamSummaryRepositoryV0_1,
)
from leo_flow.adapters.dashboard_qam_summary_backfill import (
    _PENDING_SQL,
    PostgresQamSummaryBackfillV0_1,
)
from leo_flow.contracts.core import UtcNs
from leo_flow.contracts.dashboard_capture_qam import CaptureQamSummaryQueryV0_1


class _Cursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows, self.statements = rows, []

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


def test_qam_master_projection_is_one_bounded_sql_read_with_all_states() -> None:
    rows = [
        _row("rec_complete", "complete", 1, analysis_id="slqam4_" + "a" * 32),
        _row("rec_pending", "pending", 1),
        _row("rec_unavailable", "complete", 1),
    ]
    cursor = _Cursor(rows)
    repository = PostgresCaptureQamSummaryRepositoryV0_1(
        lambda: _Connection(cursor)  # type: ignore[arg-type]
    )

    view = repository.capture_qam_summaries(
        CaptureQamSummaryQueryV0_1(UtcNs(1), UtcNs(10), 100)
    )

    assert [item.state.value for item in view.recordings] == [
        "complete",
        "pending",
        "unavailable",
    ]
    assert len(view.recordings[0].candidates) == 1
    assert cursor.statements[1][1] == (1, 10, 100)
    assert "read_dashboard_capture_qam_summaries_v0_1" in cursor.statements[1][0]
    assert len(cursor.statements) == 2  # SET READ ONLY plus one bounded projection read


def test_legacy_qam_backfill_is_explicitly_bounded_and_skips_projected_products() -> (
    None
):
    backfill = PostgresQamSummaryBackfillV0_1(
        lambda: (_ for _ in ()).throw(AssertionError("must fail before connecting")),
        object(),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="bound"):
        backfill.backfill(0)
    with pytest.raises(ValueError, match="bound"):
        backfill.backfill(101)
    assert "NOT EXISTS" in _PENDING_SQL and "LIMIT %s" in _PENDING_SQL
    assert "published_at_utc DESC" in _PENDING_SQL


def _row(
    recording_id: str,
    analysis_state: str,
    assignments: int,
    *,
    analysis_id: str | None = None,
) -> dict[str, object]:
    candidate = analysis_id is not None
    return {
        "recording_id": recording_id,
        "radio_id": "radio_a",
        "analysis_state": analysis_state,
        "assignment_count": assignments,
        "source_kind": "adaptive-v0.4" if candidate else None,
        "analysis_id": analysis_id,
        "lnb_id": "lnb_a" if candidate else None,
        "receiver_chain_id": "rx_a" if candidate else None,
        "segment_id": "seg_a" if candidate else None,
        "edge": "lower" if candidate else None,
        "qam_goodness": 0.8 if candidate else None,
        "hard_symbol_accuracy": 0.9 if candidate else None,
        "rms_evm": 0.6 if candidate else None,
        "window_count": 24 if candidate else None,
        "original_recording_count": 3,
    }
