from __future__ import annotations

from typing import Any

import pytest

from leo_flow.adapters.dashboard_master_capture_postgres import (
    MASTER_CAPTURE_SNAPSHOT_SQL,
    OBSERVATION_SNAPSHOT_SQL,
    PostgresMasterCaptureSnapshotRepositoryV0_1,
)
from leo_flow.contracts.core import (
    RadioId,
    ReceiverChainId,
    RecordingId,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.dashboard_master_capture import (
    MasterCaptureDopplerCandidateV0_1,
    MasterCaptureDopplerV0_1,
    MasterCaptureQamV0_1,
    MasterCaptureSnapshotQueryV0_1,
    MasterCaptureSummaryState,
)


class _Cursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.statements: list[tuple[str, object]] = []
        self._result: list[dict[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: str, parameters: object = None):
        self.statements.append((statement, parameters))
        if statement == MASTER_CAPTURE_SNAPSHOT_SQL:
            self._result = self.rows
        elif statement == OBSERVATION_SNAPSHOT_SQL:
            self._result = []
        return self

    def fetchall(self):
        return self._result


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


def test_postgres_snapshot_is_one_bounded_read_without_object_or_cas_access() -> None:
    cursor = _Cursor([_row(), _row(second=True)])
    repository = PostgresMasterCaptureSnapshotRepositoryV0_1(
        lambda: _Connection(cursor),  # type: ignore[arg-type]
        _MissingCanary(),
    )

    view = repository.master_capture_snapshot(
        MasterCaptureSnapshotQueryV0_1(UtcNs(1), UtcNs(100), 100)
    )

    assert len(view.items) == 1
    assert len(view.items[0].attempts) == 2
    assert view.items[0].attempts[0].capture_duration_ns == 60
    assert view.items[0].attempts[0].qam.state.value == "complete"
    assert view.items[0].attempts[1].qam.state.value == "not_analyzed"
    assert len(view.items[0].attempts[0].qam.candidates) == 1
    assert view.observation_aggregate.state.value == "complete"
    assert view.retro_qam_canary.state.value == "unavailable"
    assert len(cursor.statements) == 3
    assert "READ ONLY" in cursor.statements[0][0]
    assert cursor.statements[1][0] == MASTER_CAPTURE_SNAPSHOT_SQL
    normalized = MASTER_CAPTURE_SNAPSHOT_SQL.lower()
    assert "read_dashboard_capture_qam_summaries_v0_1" in normalized
    assert "object_blob" not in normalized
    assert "locator" not in normalized


class _MissingCanary:
    def latest_retro_qam_canary(self):
        raise LookupError("not published")


class _StoredDoppler:
    calls = 0

    def capture_doppler_snapshot(self, query, recording_ids):
        self.calls += 1
        assert query.maximum_recordings == 100
        assert recording_ids == (RecordingId("rec_1"), RecordingId("rec_2"))
        return {
            RecordingId("rec_1"): MasterCaptureDopplerV0_1(
                MasterCaptureSummaryState.COMPLETE,
                (
                    MasterCaptureDopplerCandidateV0_1(
                        RecordingId("rec_1"),
                        RadioId("radio_1"),
                        "lnb_1",
                        ReceiverChainId("rx_1"),
                        SegmentId("seg_1"),
                        "candidate_1",
                        "linear",
                        123.0,
                        0.9,
                        "doppler_1",
                        "algorithm_1",
                    ),
                ),
                (),
            )
        }


def test_stored_doppler_seam_is_one_bulk_call_and_never_per_recording() -> None:
    cursor = _Cursor([_row(), _row(second=True)])
    doppler = _StoredDoppler()
    repository = PostgresMasterCaptureSnapshotRepositoryV0_1(
        lambda: _Connection(cursor),  # type: ignore[arg-type]
        _MissingCanary(),
        doppler,
    )

    view = repository.master_capture_snapshot(
        MasterCaptureSnapshotQueryV0_1(UtcNs(1), UtcNs(100), 100)
    )

    assert doppler.calls == 1
    assert view.items[0].attempts[0].doppler.state.value == "complete"
    assert view.items[0].attempts[1].doppler.state.value == "unavailable"


class _StoredQam:
    def __init__(self, state: MasterCaptureSummaryState) -> None:
        self.state = state
        self.calls = 0

    def capture_qam_snapshot(self, query, recording_ids):
        self.calls += 1
        assert query.maximum_recordings == 100
        assert recording_ids == (RecordingId("rec_1"), RecordingId("rec_2"))
        candidates = ()
        if self.state is MasterCaptureSummaryState.COMPLETE:
            candidates = (_qam_candidate(),)
        return {
            RecordingId("rec_1"): MasterCaptureQamV0_1(
                self.state,
                candidates,
                ()
                if self.state is MasterCaptureSummaryState.COMPLETE
                else ("stored-state",),
            )
        }


@pytest.mark.parametrize(
    "state",
    [
        MasterCaptureSummaryState.COMPLETE,
        MasterCaptureSummaryState.PENDING,
        MasterCaptureSummaryState.NO_CANDIDATE,
        MasterCaptureSummaryState.NOT_ANALYZED,
        MasterCaptureSummaryState.FAILED,
    ],
)
def test_stored_qam_seam_preserves_each_terminal_state(
    state: MasterCaptureSummaryState,
) -> None:
    cursor = _Cursor([_row(), _row(second=True)])
    qam = _StoredQam(state)
    repository = PostgresMasterCaptureSnapshotRepositoryV0_1(
        lambda: _Connection(cursor),  # type: ignore[arg-type]
        _MissingCanary(),
        qam=qam,
    )

    view = repository.master_capture_snapshot(
        MasterCaptureSnapshotQueryV0_1(UtcNs(1), UtcNs(100), 100)
    )

    assert qam.calls == 1
    assert view.items[0].attempts[0].qam.state is state


def _qam_candidate():
    row = _row()
    from leo_flow.contracts.dashboard_master_capture import (
        MasterCaptureQamCandidateV0_1,
    )

    return MasterCaptureQamCandidateV0_1(
        RecordingId(str(row["recording_id"])),
        RadioId(str(row["radio_id"])),
        str(row["qam_lnb_id"]),
        ReceiverChainId(str(row["qam_receiver_chain_id"])),
        SegmentId(str(row["qam_segment_id"])),
        "lower",
        float(row["qam_goodness"]),
        float(row["qam_hard_symbol_accuracy"]),
        float(row["qam_rms_evm"]),
        int(row["qam_window_count"]),
        str(row["qam_analysis_id"]),
    )


def _row(*, second: bool = False) -> dict[str, object]:
    suffix = "2" if second else "1"
    base: dict[str, object] = {
        "projection_sequence": 10,
        "snapshot_anchor": 10,
        "batch_id": "cbatch_1",
        "mode": "coordinated",
        "coordination_claim": "measured_software_coordination",
        "capture_revision": 2,
        "requested_start_skew_ns": 0,
        "observed_start_skew_ns": 1,
        "maximum_observed_start_skew_ns": 10,
        "paired_analysis_eligibility": "eligible",
        "requested_start_utc_ns": 10,
        "attempt_position": 1 if second else 0,
        "attempt_id": f"cattempt_{suffix}",
        "radio_id": f"radio_{suffix}",
        "plan_id": f"plan_{suffix}",
        "attempt_requested_start_utc_ns": 10,
        "capture_state": "succeeded",
        "observed_start_utc_ns": 11 if second else 10,
        "recording_id": f"rec_{suffix}",
        "failure_reason": None,
        "analysis_state": "complete",
        "analysis_result_available": True,
        "capture_duration_ns": 60,
        "qam_assignment_count": 1,
        "qam_source_kind": None if second else "adaptive-v0.4",
        "qam_analysis_id": None if second else "analysis_1",
        "qam_lnb_id": None if second else "lnb_1",
        "qam_receiver_chain_id": None if second else "rx_1",
        "qam_segment_id": None if second else "seg_1",
        "qam_edge": None if second else "lower",
        "qam_goodness": None if second else 0.8,
        "qam_hard_symbol_accuracy": None if second else 0.9,
        "qam_rms_evm": None if second else 0.6,
        "qam_window_count": None if second else 24,
        "has_more": False,
    }
    return base
