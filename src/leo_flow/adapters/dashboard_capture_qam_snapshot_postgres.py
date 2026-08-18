"""Receipt-backed QAM snapshot adapter for the master capture page."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import psycopg
from psycopg.rows import dict_row

from leo_flow.contracts.core import RadioId, ReceiverChainId, RecordingId, SegmentId
from leo_flow.contracts.dashboard_master_capture import (
    MasterCaptureQamCandidateV0_1,
    MasterCaptureQamV0_1,
    MasterCaptureSnapshotQueryV0_1,
    MasterCaptureSummaryState,
)
from leo_flow.contracts.starlink import StarlinkEdge

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]

_SNAPSHOT_SQL = """
SELECT * FROM public.read_dashboard_capture_qam_snapshot_v0_1(%s,%s,%s,%s)
"""


class PostgresCaptureQamSnapshotRepositoryV0_1:
    """Read only receipt-proven terminal QAM summaries; never open CAS."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def capture_qam_snapshot(
        self,
        query: MasterCaptureSnapshotQueryV0_1,
        recording_ids: tuple[RecordingId, ...],
    ) -> Mapping[RecordingId, MasterCaptureQamV0_1]:
        if len(recording_ids) > query.maximum_recordings:
            raise ValueError("QAM snapshot recording identities exceed query bound")
        if len(recording_ids) != len(set(recording_ids)):
            raise ValueError("QAM snapshot recording identities must be unique")
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            rows = cursor.execute(
                _SNAPSHOT_SQL,
                (
                    int(query.start_utc_ns),
                    int(query.stop_utc_ns),
                    query.maximum_recordings,
                    [str(value) for value in recording_ids],
                ),
            ).fetchall()
        return _summaries(rows, recording_ids)


def _summaries(
    rows: list[dict[str, object]], requested: tuple[RecordingId, ...]
) -> Mapping[RecordingId, MasterCaptureQamV0_1]:
    requested_set = set(requested)
    grouped: dict[RecordingId, list[dict[str, object]]] = {}
    for row in rows:
        recording_id = RecordingId(str(row["recording_id"]))
        if recording_id not in requested_set:
            raise RuntimeError("QAM snapshot returned an unrequested recording")
        grouped.setdefault(recording_id, []).append(row)
    result: dict[RecordingId, MasterCaptureQamV0_1] = {}
    for recording_id, values in grouped.items():
        first = values[0]
        state = MasterCaptureSummaryState(str(first["summary_state"]))
        identity = (
            first["radio_id"],
            first["analysis_state"],
            first["source_kind"],
            first["analysis_id"],
            first["receipt_candidate_count"],
            first["assignment_count"],
            first["summary_state"],
        )
        if any(
            (
                row["radio_id"],
                row["analysis_state"],
                row["source_kind"],
                row["analysis_id"],
                row["receipt_candidate_count"],
                row["assignment_count"],
                row["summary_state"],
            )
            != identity
            for row in values
        ):
            raise RuntimeError("QAM snapshot identity changed within one recording")
        candidates = tuple(
            MasterCaptureQamCandidateV0_1(
                recording_id,
                RadioId(str(row["radio_id"])),
                str(row["lnb_id"]),
                ReceiverChainId(str(row["receiver_chain_id"])),
                SegmentId(str(row["segment_id"])),
                StarlinkEdge(str(row["edge"])),
                _number(row["qam_goodness"]),
                _number(row["hard_symbol_accuracy"]),
                _number(row["rms_evm"]),
                _integer(row["window_count"], "window_count"),
                str(row["analysis_id"]),
            )
            for row in values
            if row["segment_id"] is not None
        )
        receipt_count = _optional_integer(
            first["receipt_candidate_count"], "receipt_candidate_count"
        )
        if state is MasterCaptureSummaryState.COMPLETE and (
            receipt_count is None or receipt_count != len(candidates)
        ):
            raise RuntimeError("complete QAM receipt differs from assigned candidates")
        if state is not MasterCaptureSummaryState.COMPLETE and candidates:
            raise RuntimeError("non-complete QAM snapshot exposed candidates")
        assignment_count = _integer(first["assignment_count"], "assignment_count")
        reasons = _reasons(state, len(candidates), assignment_count)
        result[recording_id] = MasterCaptureQamV0_1(state, candidates, reasons)
    return result


def _reasons(
    state: MasterCaptureSummaryState, candidate_count: int, assignment_count: int
) -> tuple[str, ...]:
    if state is MasterCaptureSummaryState.COMPLETE:
        return (
            ("some-authoritative-receivers-have-no-published-qam",)
            if candidate_count < assignment_count
            else ()
        )
    return {
        MasterCaptureSummaryState.NO_CANDIDATE: ("qam-analysis-no-candidate",),
        MasterCaptureSummaryState.PENDING: ("qam-summary-pending",),
        MasterCaptureSummaryState.FAILED: ("analysis-failed",),
        MasterCaptureSummaryState.NOT_ANALYZED: ("qam-not-analyzed",),
    }[state]


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"database {name} is not an integer")
    return value


def _optional_integer(value: object, name: str) -> int | None:
    return None if value is None else _integer(value, name)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("database QAM metric is not numeric")
    return float(value)
