from __future__ import annotations

from typing import Any

import pytest

from leo_flow.contracts.core import RecordingId, UtcNs

master_contract = pytest.importorskip("leo_flow.contracts.dashboard_master_capture")
snapshot_adapter = pytest.importorskip(
    "leo_flow.adapters.dashboard_capture_qam_snapshot_postgres"
)
MasterCaptureSnapshotQueryV0_1 = master_contract.MasterCaptureSnapshotQueryV0_1
PostgresCaptureQamSnapshotRepositoryV0_1 = (
    snapshot_adapter.PostgresCaptureQamSnapshotRepositoryV0_1
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

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self, **kwargs: Any) -> _Cursor:
        del kwargs
        return self._cursor


def test_receipt_snapshot_maps_all_terminal_and_nonterminal_states() -> None:
    rows = [
        _row("rec_complete", "complete", candidate=True),
        _row("rec_none", "no_candidate"),
        _row("rec_pending", "pending"),
        _row("rec_failed", "failed"),
        _row("rec_fresh", "not_analyzed"),
    ]
    cursor = _Cursor(rows)
    repository = PostgresCaptureQamSnapshotRepositoryV0_1(
        lambda: _Connection(cursor)  # type: ignore[arg-type]
    )
    recording_ids = tuple(RecordingId(str(row["recording_id"])) for row in rows)

    result = repository.capture_qam_snapshot(
        MasterCaptureSnapshotQueryV0_1(UtcNs(10), UtcNs(20), 5), recording_ids
    )

    assert [result[value].state.value for value in recording_ids] == [
        "complete",
        "no_candidate",
        "pending",
        "failed",
        "not_analyzed",
    ]
    assert len(result[recording_ids[0]].candidates) == 1
    assert result[recording_ids[1]].reason_codes == ("qam-analysis-no-candidate",)
    assert len(cursor.statements) == 2
    assert "read_dashboard_capture_qam_snapshot_v0_1" in cursor.statements[1][0]
    assert cursor.statements[1][1] == (
        10,
        20,
        5,
        [str(value) for value in recording_ids],
    )


def test_receipt_snapshot_rejects_candidate_closure_or_scope_drift() -> None:
    mismatch = _row("rec_complete", "complete", candidate=True)
    mismatch["receipt_candidate_count"] = 2
    repository = PostgresCaptureQamSnapshotRepositoryV0_1(
        lambda: _Connection(_Cursor([mismatch]))  # type: ignore[arg-type]
    )
    query = MasterCaptureSnapshotQueryV0_1(UtcNs(10), UtcNs(20), 1)
    with pytest.raises(RuntimeError, match="receipt differs"):
        repository.capture_qam_snapshot(query, (RecordingId("rec_complete"),))

    repository = PostgresCaptureQamSnapshotRepositoryV0_1(
        lambda: _Connection(_Cursor([_row("rec_other", "not_analyzed")]))  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="unrequested"):
        repository.capture_qam_snapshot(query, (RecordingId("rec_complete"),))


def _row(
    recording_id: str, state: str, *, candidate: bool = False
) -> dict[str, object]:
    analysis_id = "slqam4_" + "a" * 32 if candidate else None
    return {
        "recording_id": recording_id,
        "radio_id": "radio_a",
        "analysis_state": "complete",
        "summary_state": state,
        "assignment_count": 1,
        "source_kind": "adaptive-v0.4" if candidate else None,
        "analysis_id": analysis_id,
        "receipt_candidate_count": 1
        if candidate
        else (0 if state == "no_candidate" else None),
        "lnb_id": "lnb_a" if candidate else None,
        "receiver_chain_id": "rx_a" if candidate else None,
        "segment_id": "seg_a" if candidate else None,
        "edge": "lower" if candidate else None,
        "qam_goodness": 0.8 if candidate else None,
        "hard_symbol_accuracy": 0.9 if candidate else None,
        "rms_evm": 0.2 if candidate else None,
        "window_count": 3 if candidate else None,
    }
