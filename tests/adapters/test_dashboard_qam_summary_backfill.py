from __future__ import annotations

from typing import Any, Self

import pytest

from leo_flow.adapters.dashboard_qam_summary_backfill import (
    PostgresQamSummaryBackfillV0_2,
)


class _Cursor:
    def __init__(self, *, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = rows or []
        self.statements: list[tuple[str, object]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: str, parameters: object = None) -> _Cursor:
        self.statements.append((statement, parameters))
        return self

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows

    def fetchone(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self, **kwargs: Any) -> _Cursor:
        del kwargs
        return self._cursor


def test_exact_pending_identity_builds_cataloged_ref_without_constructing_a_path() -> None:
    product = _source_row()
    cursor = _Cursor(rows=[product])
    backfill = PostgresQamSummaryBackfillV0_2(
        lambda: _Connection(cursor),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    ref = backfill._adaptive_ref(_pending())

    assert ref is not None
    assert ref.analysis_id == "slqam4_" + "1" * 32
    assert ref.bundle_ref.locator == "opaque-catalog-locator"
    exact = cursor.statements[-1]
    assert "read_exact_recording_starlink_adaptive_qam_v0_4" in exact[0]
    assert exact[1] == (
        "slqam4_" + "1" * 32,
        "rec_qam_backfill",
        "sha256",
        "3" * 64,
    )


def test_pending_request_closure_mismatch_fails_closed() -> None:
    product = {**_source_row(), "request_digest_value": "f" * 64}
    backfill = PostgresQamSummaryBackfillV0_2(
        lambda: _Connection(_Cursor(rows=[product])),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="closure differs"):
        backfill._adaptive_ref(_pending())


def test_missing_exact_science_product_is_skipped_without_publication() -> None:
    pending_cursor = _Cursor(rows=[_pending()])
    missing_cursor = _Cursor()
    cursors = iter((pending_cursor, missing_cursor))
    backfill = PostgresQamSummaryBackfillV0_2(
        lambda: _Connection(next(cursors)),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    assert backfill.backfill(1) == 0
    assert len(pending_cursor.statements) == 1
    assert len(missing_cursor.statements) == 2
    assert all(
        "publish_dashboard_capture_qam_summary_receipt" not in statement
        for statement, _parameters in missing_cursor.statements
    )


def _pending() -> dict[str, object]:
    return {
        "source_kind": "adaptive-v0.4",
        "analysis_id": "slqam4_" + "1" * 32,
        "recording_id": "rec_qam_backfill",
        "source_request_digest_value": "2" * 64,
        "source_product_digest_value": "3" * 64,
    }


def _source_row() -> dict[str, object]:
    return {
        "analysis_id": "slqam4_" + "1" * 32,
        "recording_id": "rec_qam_backfill",
        "request_digest_value": "2" * 64,
        "bundle_digest_algorithm": "sha256",
        "bundle_digest_value": "3" * 64,
        "bundle_byte_count": 123,
        "bundle_media_type": "application/json",
        "bundle_format_id": "starlink-adaptive-qam-v0.4",
        "bundle_locator": "opaque-catalog-locator",
    }
