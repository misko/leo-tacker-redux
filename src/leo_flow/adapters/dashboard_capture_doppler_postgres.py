"""Bounded PostgreSQL scope for master-table Doppler summaries."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row

from leo_flow.contracts.core import (
    RadioId,
    ReceiverChainId,
    RecordingId,
    SegmentId,
)
from leo_flow.contracts.dashboard_capture_doppler import (
    CaptureDopplerCandidateSummaryV0_1,
    CaptureDopplerHardwareAssignmentV0_1,
    CaptureDopplerScopeRecordingV0_1,
    CaptureDopplerScopeViewV0_1,
    CaptureDopplerSummaryQueryV0_1,
)
from leo_flow.contracts.dashboard_master_capture import (
    MasterCaptureDopplerV0_1,
    MasterCaptureSnapshotQueryV0_1,
    MasterCaptureSummaryState,
)

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresCaptureDopplerScopeRepositoryV0_1:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def capture_doppler_scope(
        self, query: CaptureDopplerSummaryQueryV0_1
    ) -> CaptureDopplerScopeViewV0_1:
        return self._capture_scope(query, _SCOPE_SQL)

    def _capture_scope(
        self, query: CaptureDopplerSummaryQueryV0_1, scope_sql: str
    ) -> CaptureDopplerScopeViewV0_1:
        parameters = {
            "start_utc_ns": int(query.start_utc_ns),
            "stop_utc_ns": int(query.stop_utc_ns),
            "limit": query.maximum_recordings,
        }
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(_COUNT_SQL, parameters)
            count_row = cursor.fetchone()
            if count_row is None:
                raise RuntimeError("capture Doppler scope count returned no row")
            cursor.execute(scope_sql, parameters)
            rows = cursor.fetchall()
        grouped: dict[str, dict[str, object]] = {}
        for row in rows:
            recording_id = str(row["recording_id"])
            entry = grouped.setdefault(
                recording_id,
                {
                    "radio_id": str(row["radio_id"]),
                    "analysis_state": str(row["analysis_state"]),
                    "assignments": [],
                },
            )
            if entry["radio_id"] != str(row["radio_id"]):
                raise RuntimeError("capture Doppler recording radio identity changed")
            receiver = row["receiver_chain_id"]
            lnb = row["lnb_id"]
            if receiver is not None and lnb is not None:
                assignments = entry["assignments"]
                if not isinstance(assignments, list):
                    raise TypeError("capture Doppler assignments are malformed")
                assignments.append(
                    CaptureDopplerHardwareAssignmentV0_1(
                        ReceiverChainId(str(receiver)), str(lnb)
                    )
                )
        recordings = tuple(
            CaptureDopplerScopeRecordingV0_1(
                RecordingId(recording_id),
                RadioId(str(value["radio_id"])),
                str(value["analysis_state"]),
                tuple(value["assignments"]),  # type: ignore[arg-type]
            )
            for recording_id, value in grouped.items()
        )
        original = _integer(count_row["recording_count"], "recording_count")
        return CaptureDopplerScopeViewV0_1(
            recordings, original, original > len(recordings)
        )


class PostgresCaptureQamScopeRepositoryV0_1(
    PostgresCaptureDopplerScopeRepositoryV0_1
):
    """Prefer recent recordings with published QAM inside the UI's small bound."""

    def capture_doppler_scope(
        self, query: CaptureDopplerSummaryQueryV0_1
    ) -> CaptureDopplerScopeViewV0_1:
        return self._capture_scope(query, _QAM_SCOPE_SQL)


class PostgresCaptureDopplerSnapshotRepositoryV0_1:
    """Read normalized Doppler receipts without opening analysis objects."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def capture_doppler_snapshot(
        self,
        query: MasterCaptureSnapshotQueryV0_1,
        recording_ids: tuple[RecordingId, ...],
    ) -> Mapping[RecordingId, MasterCaptureDopplerV0_1]:
        requested = frozenset(recording_ids)
        if len(requested) != len(recording_ids):
            raise ValueError("capture Doppler requested recordings must be unique")
        if len(requested) > query.maximum_recordings:
            raise ValueError("capture Doppler requested recording closure exceeds bound")
        if not recording_ids:
            return {}
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            rows = cursor.execute(
                """
                SELECT *
                  FROM public.read_dashboard_capture_doppler_summaries_v0_1(
                           %s, %s, %s
                       )
                 WHERE recording_id = ANY(%s::text[])
                 ORDER BY recording_id, lnb_id, receiver_chain_id
                """,
                (
                    int(query.start_utc_ns),
                    int(query.stop_utc_ns),
                    query.maximum_recordings,
                    [str(item) for item in recording_ids],
                ),
            ).fetchall()
        grouped: dict[RecordingId, list[dict[str, object]]] = {}
        for row in rows:
            recording_id = RecordingId(_text(row, "recording_id"))
            if recording_id not in requested:
                raise RuntimeError("capture Doppler recording closure differs")
            grouped.setdefault(recording_id, []).append(row)
        return {
            recording_id: _master_doppler(recording_id, items)
            for recording_id, items in grouped.items()
        }


_LATEST_SUCCESSFUL = """
WITH latest_batches AS (
    SELECT DISTINCT ON (batch_id) projection_sequence, batch_id,
           requested_start_utc_ns
      FROM public.dashboard_capture_batch_projection
     WHERE requested_start_utc_ns >= %(start_utc_ns)s
       AND requested_start_utc_ns < %(stop_utc_ns)s
     ORDER BY batch_id, projection_sequence DESC
), successful AS (
    SELECT DISTINCT ON (attempt.recording_id)
           attempt.recording_id, attempt.radio_id, attempt.analysis_state,
           attempt.observed_start_utc_ns
      FROM latest_batches AS batch
      JOIN public.dashboard_capture_attempt_projection AS attempt
        ON attempt.projection_sequence = batch.projection_sequence
     WHERE attempt.capture_state = 'succeeded'
       AND attempt.recording_id IS NOT NULL
     ORDER BY attempt.recording_id, batch.requested_start_utc_ns DESC
)
"""

_COUNT_SQL = _LATEST_SUCCESSFUL + "SELECT count(*) AS recording_count FROM successful"

_SCOPE_SQL = (
    _LATEST_SUCCESSFUL
    + """
, bounded AS (
    SELECT * FROM successful ORDER BY recording_id LIMIT %(limit)s
)
SELECT bounded.recording_id, bounded.radio_id, bounded.analysis_state,
       chain.receiver_chain_id, chain.lnb_id, chain.chain_index
  FROM bounded
  LEFT JOIN public.recording_hardware_link AS link
    ON link.recording_id = bounded.recording_id
  LEFT JOIN public.hardware_receiver_chain AS chain
    ON chain.snapshot_id = link.hardware_snapshot_id
   AND chain.radio_id = bounded.radio_id
   AND chain.valid_from_utc_ns <= bounded.observed_start_utc_ns
   AND (chain.valid_until_utc_ns IS NULL
        OR bounded.observed_start_utc_ns < chain.valid_until_utc_ns)
 ORDER BY bounded.recording_id, chain.chain_index
"""
)

_QAM_SCOPE_SQL = (
    _LATEST_SUCCESSFUL
    + """
, ranked AS (
    SELECT successful.*,
           (EXISTS (
                SELECT 1
                  FROM public.read_latest_recording_starlink_adaptive_qam_v0_4(
                           successful.recording_id
                       )
            ) OR EXISTS (
                SELECT 1
                  FROM public.read_latest_recording_starlink_acquired_constellation_v0_3(
                           successful.recording_id
                       )
            )) AS has_published_qam
      FROM successful
), bounded AS (
    SELECT * FROM ranked
     ORDER BY has_published_qam DESC, observed_start_utc_ns DESC, recording_id
     LIMIT %(limit)s
)
SELECT bounded.recording_id, bounded.radio_id, bounded.analysis_state,
       chain.receiver_chain_id, chain.lnb_id, chain.chain_index
  FROM bounded
  LEFT JOIN public.recording_hardware_link AS link
    ON link.recording_id = bounded.recording_id
  LEFT JOIN public.hardware_receiver_chain AS chain
    ON chain.snapshot_id = link.hardware_snapshot_id
   AND chain.radio_id = bounded.radio_id
   AND chain.valid_from_utc_ns <= bounded.observed_start_utc_ns
   AND (chain.valid_until_utc_ns IS NULL
        OR bounded.observed_start_utc_ns < chain.valid_until_utc_ns)
 ORDER BY bounded.recording_id, chain.chain_index
"""
)


_DOPPLER_STATES = {
    "complete": MasterCaptureSummaryState.COMPLETE,
    "pending": MasterCaptureSummaryState.PENDING,
    "no_candidate": MasterCaptureSummaryState.NO_CANDIDATE,
    "not_analyzed": MasterCaptureSummaryState.NOT_ANALYZED,
    "failed": MasterCaptureSummaryState.FAILED,
}

_DOPPLER_REASONS = {
    MasterCaptureSummaryState.PENDING: ("doppler-analysis-pending",),
    MasterCaptureSummaryState.NO_CANDIDATE: ("no-published-doppler-candidate",),
    MasterCaptureSummaryState.NOT_ANALYZED: ("doppler-analysis-not-analyzed",),
    MasterCaptureSummaryState.FAILED: ("doppler-analysis-failed",),
}


def _master_doppler(
    recording_id: RecordingId, rows: list[dict[str, object]]
) -> MasterCaptureDopplerV0_1:
    first = rows[0]
    radio_id = RadioId(_text(first, "radio_id"))
    summary_state = _text(first, "summary_state")
    try:
        state = _DOPPLER_STATES[summary_state]
    except KeyError as error:
        raise RuntimeError("capture Doppler summary state is unsupported") from error
    assignment_count = _integer(first["assignment_count"], "assignment_count")
    if assignment_count < 0:
        raise RuntimeError("capture Doppler assignment count is negative")
    for row in rows[1:]:
        if (
            _text(row, "radio_id") != str(radio_id)
            or _text(row, "summary_state") != summary_state
            or _integer(row["assignment_count"], "assignment_count")
            != assignment_count
        ):
            raise RuntimeError("capture Doppler recording summary is inconsistent")
    candidates = tuple(
        _doppler_candidate(recording_id, radio_id, row)
        for row in rows
        if row["candidate_id"] is not None
    )
    if len(candidates) > assignment_count:
        raise RuntimeError("capture Doppler candidates exceed hardware assignments")
    reasons: tuple[str, ...]
    if state is MasterCaptureSummaryState.COMPLETE:
        reasons = (
            ()
            if len(candidates) == assignment_count
            else ("some-authoritative-receivers-have-no-published-doppler",)
        )
    else:
        reasons = _DOPPLER_REASONS[state]
    return MasterCaptureDopplerV0_1(state, candidates, reasons)


def _doppler_candidate(
    recording_id: RecordingId,
    radio_id: RadioId,
    row: dict[str, object],
) -> CaptureDopplerCandidateSummaryV0_1:
    return CaptureDopplerCandidateSummaryV0_1(
        recording_id,
        radio_id,
        _text(row, "lnb_id"),
        ReceiverChainId(_text(row, "receiver_chain_id")),
        SegmentId(_text(row, "segment_id")),
        _text(row, "candidate_id"),
        _text(row, "model"),
        float(cast(Any, row["drift_rate_hz_s"])),
        float(cast(Any, row["ranking_score"])),
        _text(row, "doppler_id"),
        _text(row, "algorithm_version"),
    )


def _text(row: dict[str, object], name: str) -> str:
    value = row[name]
    if not isinstance(value, str):
        raise TypeError(f"database {name} is not text")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"database {name} is not an integer")
    return value
