from __future__ import annotations

from typing import Self

import pytest

from leo_flow.adapters.supercycle_canary_closure_postgres import (
    PostgresSupercycleCanaryClosureReaderV1,
)
from leo_flow.contracts.core import CaptureBatchId, Digest, JobId, RecordingId
from leo_flow.contracts.deferred_analysis import DeferredAnalysisWindowV1


def _window() -> DeferredAnalysisWindowV1:
    return DeferredAnalysisWindowV1(
        Digest.sha256(b"canary-definition"),
        0,
        tuple(CaptureBatchId(f"cbatch_canary_{index:02d}") for index in range(36)),
        tuple(RecordingId(f"rec_canary_{index:02d}") for index in range(72)),
        tuple(Digest.sha256(f"recording-{index}".encode()) for index in range(72)),
        tuple(JobId(f"job_feature_{index:02d}") for index in range(72)),
        tuple(JobId(f"job_waterfall_{index:02d}") for index in range(72)),
        tuple(JobId(f"job_suite_{index:02d}") for index in range(72)),
    )


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def fetchone(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, object]]:
        return self._rows


class _Connection:
    def __init__(self, *, missing_suite: bool = False) -> None:
        self.missing_suite = missing_suite
        self.calls: list[tuple[str, object]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: object) -> _Result:
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        if "read_feature_projection_receipt" in normalized:
            return _Result([{"count": 72, "recording_count": 72}])
        if "read_waterfall_analysis_receipt" in normalized:
            return _Result([{"count": 72, "recording_count": 72}])
        if "read_starlink_detector_suite_receipt" in normalized:
            limit = 71 if self.missing_suite else 72
            return _Result(
                [
                    {
                        "recording_id": f"rec_canary_{index:02d}",
                        "analysis_id": f"slsuite_{index:032x}",
                    }
                    for index in range(limit)
                ]
            )
        return _Result([{"count": 72}])


def test_closure_reads_only_exact_window_receipts_and_v4_pairs() -> None:
    analysis, dashboard = _Connection(), _Connection()
    window = _window()
    reader = PostgresSupercycleCanaryClosureReaderV1(
        lambda: analysis, lambda: dashboard
    )

    assert reader.counts(str(window.definition_digest), window) == (72, 72, 72, 72)
    assert len(analysis.calls) == 3
    assert len(dashboard.calls) == 1
    assert all("unnest" in sql for sql, _params in analysis.calls)
    assert (
        "dashboard_recording_starlink_detector_suite_projection"
        in dashboard.calls[0][0]
    )


def test_closure_fails_closed_before_dashboard_when_suite_is_incomplete() -> None:
    analysis, dashboard = _Connection(missing_suite=True), _Connection()
    window = _window()
    reader = PostgresSupercycleCanaryClosureReaderV1(
        lambda: analysis, lambda: dashboard
    )

    assert reader.counts(str(window.definition_digest), window) == (72, 72, 71, 0)
    assert dashboard.calls == []


def test_closure_rejects_definition_substitution() -> None:
    reader = PostgresSupercycleCanaryClosureReaderV1(_Connection, _Connection)
    window = _window()

    with pytest.raises(ValueError, match="definition digest"):
        reader.counts(str(Digest.sha256(b"other")), window)
