from __future__ import annotations

from typing import Any

from leo_flow.adapters.dashboard_qam_summary_projection import (
    _best_per_receiver,
    _publish,
)


class _Cursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []

    def execute(self, statement: str, parameters: object = None) -> _Cursor:
        self.statements.append((statement, parameters))
        return self

    def fetchone(self) -> dict[str, object]:
        return {"published": True}


def test_empty_candidate_set_publishes_explicit_terminal_receipt() -> None:
    cursor = _Cursor()

    _publish(
        cursor,  # type: ignore[arg-type]
        "acquired-v0.3",
        "slqam3rec_" + "1" * 32,
        [],
    )

    assert len(cursor.statements) == 1
    statement, parameters = cursor.statements[0]
    assert "publish_dashboard_capture_qam_summary_receipt_v0_2" in statement
    assert parameters is not None
    values = parameters if isinstance(parameters, tuple) else ()
    assert values[0:2] == (
        "acquired-v0.3",
        "slqam3rec_" + "1" * 32,
    )
    assert "no-candidate" in values


def test_candidate_rows_are_sent_as_one_unpooled_terminal_publication() -> None:
    cursor = _Cursor()
    rows: list[dict[str, Any]] = [
        {
            "source_kind": "adaptive-v0.4",
            "analysis_id": "slqam4_" + "2" * 32,
            "recording_id": "rec_qam_summary_receipt",
            "radio_id": "radio_a",
            "lnb_id": "lnb_a",
            "receiver_chain_id": "rx_a",
            "segment_id": "seg_a",
            "edge": "lower",
            "qam_goodness": 0.8,
            "hard_symbol_accuracy": 0.9,
            "rms_evm": 0.6,
            "window_count": 2,
        }
    ]

    _publish(cursor, "adaptive-v0.4", "slqam4_" + "2" * 32, rows)  # type: ignore[arg-type]

    statement, parameters = cursor.statements[0]
    assert "publish_dashboard_capture_qam_summary_receipt_v0_2" in statement
    values = parameters if isinstance(parameters, tuple) else ()
    assert "complete" in values
    assert values[-1].obj == rows


def test_candidate_selection_never_pools_radio_lnb_receiver_streams() -> None:
    rows = [
        _candidate("radio_a", "lnb_a", "rx_a", 0.6),
        _candidate("radio_a", "lnb_a", "rx_a", 0.8),
        _candidate("radio_b", "lnb_a", "rx_a", 0.7),
        _candidate("radio_a", "lnb_b", "rx_a", 0.5),
        _candidate("radio_a", "lnb_a", "rx_b", 0.4),
    ]

    selected = _best_per_receiver(rows)

    assert [
        (row["radio_id"], row["lnb_id"], row["receiver_chain_id"])
        for row in selected
    ] == [
        ("radio_a", "lnb_a", "rx_a"),
        ("radio_a", "lnb_a", "rx_b"),
        ("radio_a", "lnb_b", "rx_a"),
        ("radio_b", "lnb_a", "rx_a"),
    ]
    assert selected[0]["qam_goodness"] == 0.8


def _candidate(
    radio_id: str, lnb_id: str, receiver_chain_id: str, goodness: float
) -> dict[str, object]:
    return {
        "radio_id": radio_id,
        "lnb_id": lnb_id,
        "receiver_chain_id": receiver_chain_id,
        "segment_id": "seg_a",
        "edge": "lower",
        "qam_goodness": goodness,
    }
