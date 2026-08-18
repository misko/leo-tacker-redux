"""Bounded PostgreSQL scope for master-table Doppler summaries."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from leo_flow.contracts.core import RadioId, ReceiverChainId, RecordingId
from leo_flow.contracts.dashboard_capture_doppler import (
    CaptureDopplerHardwareAssignmentV0_1,
    CaptureDopplerScopeRecordingV0_1,
    CaptureDopplerScopeViewV0_1,
    CaptureDopplerSummaryQueryV0_1,
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


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"database {name} is not an integer")
    return value
