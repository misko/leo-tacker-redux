from __future__ import annotations

from pathlib import Path

import pytest

from leo_flow.adapters.focused_continuous_sqlite import (
    FocusedContinuousRecordV0_1,
    SQLiteFocusedContinuousJournalV0_1,
)


def _record(root: Path, sequence: int = 0) -> FocusedContinuousRecordV0_1:
    return FocusedContinuousRecordV0_1(
        sequence,
        f"focused_loop_{sequence:08d}_abc",
        123 + sequence,
        "sha256:" + "a" * 64,
        root / f"dwell-{sequence}",
        f"cbatch_focused_loop_{sequence:08d}_abc_u000",
        "planned",
    )


def test_full_sync_journal_closes_capture_and_analysis_transitions(
    tmp_path: Path,
) -> None:
    journal = SQLiteFocusedContinuousJournalV0_1(tmp_path / "journal.sqlite3")
    record = _record(tmp_path)
    journal.insert_planned(record)
    assert journal.next_sequence() == 1
    assert journal.incomplete() == (record,)

    journal.transition(0, "planned", "captured")
    journal.transition(0, "captured", "analysis_running")
    journal.transition(0, "analysis_running", "complete")

    assert journal.incomplete() == ()
    assert journal.get(0) is not None
    assert journal.get(0).state == "complete"  # type: ignore[union-attr]


def test_journal_rejects_conflicting_replay(tmp_path: Path) -> None:
    journal = SQLiteFocusedContinuousJournalV0_1(tmp_path / "journal.sqlite3")
    journal.insert_planned(_record(tmp_path))
    journal.transition(0, "planned", "captured")
    with pytest.raises(RuntimeError, match="transition conflict"):
        journal.transition(0, "planned", "captured")


def test_journal_preserves_failed_terminal_reason(tmp_path: Path) -> None:
    journal = SQLiteFocusedContinuousJournalV0_1(tmp_path / "journal.sqlite3")
    journal.insert_planned(_record(tmp_path))
    journal.transition(0, "planned", "failed", error="capture-uncertain")
    record = journal.get(0)
    assert record is not None
    assert record.state == "failed"
    assert record.error == "capture-uncertain"
    assert journal.incomplete() == ()
