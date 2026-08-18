from __future__ import annotations

from typing import Self

from leo_flow.adapters.dashboard_recording_evidence_postgres import (
    PostgresRecordingEvidenceContextRepositoryV0_1,
)
from leo_flow.contracts.core import CaptureBatchId, RecordingId


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, object]]:
        return self._rows


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(
        self, statement: str, parameters: dict[str, object] | None = None
    ) -> _Result:
        normalized = " ".join(statement.split())
        self.calls.append((normalized, parameters))
        if normalized.startswith("WITH latest_batch"):
            return _Result([{"batch_id": "cbatch_a"}, {"batch_id": "cbatch_b"}])
        return _Result([])


def test_batch_lookup_uses_bounded_dashboard_projection_reads() -> None:
    connection = _Connection()
    repository = PostgresRecordingEvidenceContextRepositoryV0_1(
        lambda: connection,
        object(),
        object(),  # type: ignore[arg-type]
    )

    assert repository._batch_ids(RecordingId("rec_target")) == (
        CaptureBatchId("cbatch_a"),
        CaptureBatchId("cbatch_b"),
    )

    assert connection.calls[0] == ("SET TRANSACTION READ ONLY", None)
    statement, parameters = connection.calls[1]
    assert "dashboard_capture_batch_projection" in statement
    assert "dashboard_capture_attempt_projection" in statement
    assert "resolve_dashboard_capture_batches_for_recording" not in statement
    assert statement.endswith("LIMIT 2")
    assert parameters == {"recording_id": "rec_target"}
