from __future__ import annotations

import sqlite3
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
    with pytest.raises(ValueError, match="must change state"):
        journal.transition(0, "captured", "captured")


def test_analysis_process_claim_and_proven_dead_recovery_are_explicit(
    tmp_path: Path,
) -> None:
    journal = SQLiteFocusedContinuousJournalV0_1(tmp_path / "journal.sqlite3")
    journal.insert_planned(_record(tmp_path))
    journal.transition(0, "planned", "captured")
    journal.claim_analysis_process(
        0,
        pid=123,
        process_start_ticks=456,
        command_digest="sha256:" + "b" * 64,
    )
    running = journal.get(0)
    assert running is not None
    assert (
        running.state,
        running.analysis_pid,
        running.analysis_process_start_ticks,
    ) == (
        "analysis_running",
        123,
        456,
    )

    journal.abandon_analysis_process(0)
    captured = journal.get(0)
    assert captured is not None
    assert captured.state == "captured"
    assert captured.analysis_pid is None
    assert captured.analysis_command_digest is None


def test_journal_preserves_failed_terminal_reason(tmp_path: Path) -> None:
    journal = SQLiteFocusedContinuousJournalV0_1(tmp_path / "journal.sqlite3")
    journal.insert_planned(_record(tmp_path))
    journal.transition(0, "planned", "failed", error="capture-uncertain")
    record = journal.get(0)
    assert record is not None
    assert record.state == "failed"
    assert record.error == "capture-uncertain"
    assert journal.incomplete() == ()


def test_existing_journal_is_additively_upgraded_for_process_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE focused_dwell(
            sequence INTEGER PRIMARY KEY,monitor_id TEXT NOT NULL UNIQUE,
            requested_start_utc_ns INTEGER NOT NULL,definition_digest TEXT NOT NULL,
            state_root TEXT NOT NULL UNIQUE,batch_id TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL,error TEXT,revision INTEGER NOT NULL DEFAULT 0
            ) STRICT"""
        )
    journal = SQLiteFocusedContinuousJournalV0_1(path)
    journal.insert_planned(_record(tmp_path))
    journal.transition(0, "planned", "captured")
    journal.claim_analysis_process(
        0,
        pid=10,
        process_start_ticks=20,
        command_digest="sha256:" + "c" * 64,
    )
    assert journal.get(0).analysis_pid == 10  # type: ignore[union-attr]
