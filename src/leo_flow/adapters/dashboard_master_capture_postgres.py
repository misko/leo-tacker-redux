"""Stored, bounded page-load snapshot for the dashboard capture table."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from typing import Any, Final, cast

import psycopg
from psycopg.rows import dict_row

from leo_flow.adapters.dashboard_observation_postgres import (
    aggregate_observation_rows_v0_1,
)
from leo_flow.adapters.dashboard_observation_postgres_sql import OBSERVATION_ROWS_SQL
from leo_flow.contracts.capture_batch import (
    CaptureBatchMode,
    PairedAnalysisEligibility,
)
from leo_flow.contracts.core import (
    CaptureAttemptId,
    CaptureBatchId,
    PlanId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.dashboard import TimeRangeQuery
from leo_flow.contracts.dashboard_batch import (
    CoordinationClaim,
    DashboardAnalysisState,
    DashboardCaptureState,
)
from leo_flow.contracts.dashboard_master_capture import (
    CaptureDopplerSnapshotQueryPortV0_1,
    CaptureQamSnapshotQueryPortV0_1,
    MasterCaptureAttemptV0_1,
    MasterCaptureBatchV0_1,
    MasterCaptureDopplerV0_1,
    MasterCaptureObservationV0_1,
    MasterCapturePilotV0_1,
    MasterCaptureQamCandidateV0_1,
    MasterCaptureQamV0_1,
    MasterCaptureRetroQamCanaryV0_1,
    MasterCaptureSatelliteV0_1,
    MasterCaptureSnapshotQueryV0_1,
    MasterCaptureSnapshotV0_1,
    MasterCaptureSummaryState,
)
from leo_flow.contracts.dashboard_retro_qam_canary import (
    RetroQamCanaryDashboardQueryPortV0_1,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.dashboard.repository import InvalidCursor

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]
_CURSOR_VERSION: Final = 1
_MAX_OBSERVATION_ROWS: Final = 10_000

MASTER_CAPTURE_SNAPSHOT_SQL = """
WITH snapshot AS (
    SELECT COALESCE(
               %(anchor)s::bigint,
               (SELECT MAX(projection_sequence)
                  FROM public.dashboard_capture_batch_projection),
               -1
           ) AS anchor
), latest_batch AS (
    SELECT DISTINCT ON (batch.batch_id)
           batch.projection_sequence, batch.batch_id, batch.capture_revision,
           batch.mode, batch.coordination_claim, batch.requested_start_utc_ns,
           batch.requested_start_skew_ns, batch.observed_start_skew_ns,
           batch.maximum_observed_start_skew_ns,
           batch.paired_analysis_eligibility
      FROM public.dashboard_capture_batch_projection AS batch, snapshot
     WHERE batch.projection_sequence <= snapshot.anchor
     ORDER BY batch.batch_id, batch.projection_sequence DESC
), bounded_batch AS (
    SELECT latest_batch.*,
           row_number() OVER (
               ORDER BY requested_start_utc_ns DESC, batch_id DESC
           ) AS page_position
      FROM latest_batch
     WHERE requested_start_utc_ns >= %(start_utc_ns)s
       AND requested_start_utc_ns < %(stop_utc_ns)s
       AND (%(after_started)s::bigint IS NULL
            OR (requested_start_utc_ns, batch_id)
               < (%(after_started)s::bigint, %(after_id)s::text))
     ORDER BY requested_start_utc_ns DESC, batch_id DESC
     LIMIT %(batch_limit_plus_one)s
), selected_batch AS (
    SELECT * FROM bounded_batch WHERE page_position <= %(batch_limit)s
), latest_detail AS (
    SELECT DISTINCT ON (detail.recording_id)
           detail.recording_id, detail.semantic_view
      FROM public.dashboard_recording_detail_projection AS detail
     ORDER BY detail.recording_id, detail.projection_sequence DESC
), qam AS (
    SELECT *
      FROM public.read_dashboard_capture_qam_summaries_v0_1(
               %(start_utc_ns)s, %(stop_utc_ns)s, %(maximum_recordings)s
           )
)
SELECT snapshot.anchor AS snapshot_anchor,
       EXISTS (
           SELECT 1 FROM bounded_batch WHERE page_position > %(batch_limit)s
       ) AS has_more,
       batch.projection_sequence, batch.batch_id, batch.capture_revision,
       batch.mode, batch.coordination_claim, batch.requested_start_utc_ns,
       batch.requested_start_skew_ns, batch.observed_start_skew_ns,
       batch.maximum_observed_start_skew_ns,
       batch.paired_analysis_eligibility,
       attempt.attempt_position, attempt.attempt_id, attempt.radio_id,
       attempt.plan_id,
       attempt.requested_start_utc_ns AS attempt_requested_start_utc_ns,
       attempt.capture_state, attempt.observed_start_utc_ns,
       attempt.recording_id, attempt.failure_reason, attempt.analysis_state,
       attempt.analysis_result_available,
       CASE WHEN detail.semantic_view IS NULL THEN NULL
            ELSE (detail.semantic_view->>'capture_finished_utc_ns')::bigint
                 - (detail.semantic_view->>'capture_started_utc_ns')::bigint
        END AS capture_duration_ns,
       qam.assignment_count AS qam_assignment_count,
       qam.source_kind AS qam_source_kind,
       qam.analysis_id AS qam_analysis_id,
       qam.lnb_id AS qam_lnb_id,
       qam.receiver_chain_id AS qam_receiver_chain_id,
       qam.segment_id AS qam_segment_id,
       qam.edge AS qam_edge,
       qam.qam_goodness, qam.hard_symbol_accuracy AS qam_hard_symbol_accuracy,
       qam.rms_evm AS qam_rms_evm, qam.window_count AS qam_window_count
  FROM snapshot
  JOIN selected_batch AS batch ON TRUE
  JOIN public.dashboard_capture_attempt_projection AS attempt
    ON attempt.projection_sequence = batch.projection_sequence
  LEFT JOIN latest_detail AS detail
    ON detail.recording_id = attempt.recording_id
  LEFT JOIN qam ON qam.recording_id = attempt.recording_id
 ORDER BY batch.requested_start_utc_ns DESC, batch.batch_id DESC,
          attempt.attempt_position, qam.lnb_id, qam.receiver_chain_id
"""

OBSERVATION_SNAPSHOT_SQL = OBSERVATION_ROWS_SQL + "\n LIMIT %(observation_limit)s"


class PostgresMasterCaptureSnapshotRepositoryV0_1:
    """Read all table evidence without opening recording or analysis objects."""

    def __init__(
        self,
        connect: ConnectionFactory,
        canary: RetroQamCanaryDashboardQueryPortV0_1,
        doppler: CaptureDopplerSnapshotQueryPortV0_1 | None = None,
        qam: CaptureQamSnapshotQueryPortV0_1 | None = None,
    ) -> None:
        self._connect = connect
        self._canary = canary
        self._doppler = doppler
        self._qam = qam

    def master_capture_snapshot(
        self, query: MasterCaptureSnapshotQueryV0_1, cursor: str | None = None
    ) -> MasterCaptureSnapshotV0_1:
        fingerprint = _fingerprint(query)
        state = _decode_cursor(cursor, fingerprint)
        after = None if state is None else cast(list[object], state["after"])
        batch_limit = max(1, query.maximum_recordings // 2)
        parameters = {
            "anchor": None if state is None else state["anchor"],
            "start_utc_ns": int(query.start_utc_ns),
            "stop_utc_ns": int(query.stop_utc_ns),
            "after_started": None if after is None else after[0],
            "after_id": None if after is None else after[1],
            "batch_limit": batch_limit,
            "batch_limit_plus_one": batch_limit + 1,
            "maximum_recordings": query.maximum_recordings,
        }
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as db,
        ):
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            rows = db.execute(MASTER_CAPTURE_SNAPSHOT_SQL, parameters).fetchall()
            observation_rows = db.execute(
                OBSERVATION_SNAPSHOT_SQL,
                {
                    "start_utc_ns": int(query.start_utc_ns),
                    "stop_utc_ns": int(query.stop_utc_ns),
                    "radio_ids": [],
                    "observation_limit": _MAX_OBSERVATION_ROWS + 1,
                },
            ).fetchall()
        if len(observation_rows) > _MAX_OBSERVATION_ROWS:
            raise RuntimeError("observation aggregate exceeds its stored row bound")
        observation = aggregate_observation_rows_v0_1(
            TimeRangeQuery(query.start_utc_ns, query.stop_utc_ns), observation_rows
        )
        recording_ids = tuple(
            sorted(
                {
                    RecordingId(str(row["recording_id"]))
                    for row in rows
                    if row["recording_id"] is not None
                }
            )
        )
        doppler = (
            {}
            if self._doppler is None
            else self._doppler.capture_doppler_snapshot(query, recording_ids)
        )
        if not set(doppler) <= set(recording_ids):
            raise RuntimeError("stored Doppler snapshot returned another recording")
        qam = (
            {}
            if self._qam is None
            else self._qam.capture_qam_snapshot(query, recording_ids)
        )
        if not set(qam) <= set(recording_ids):
            raise RuntimeError("stored QAM snapshot returned another recording")
        batches = _batches(rows, doppler, qam)
        next_cursor = _next_cursor(rows, batches, fingerprint)
        try:
            canary = MasterCaptureRetroQamCanaryV0_1(
                MasterCaptureSummaryState.COMPLETE,
                self._canary.latest_retro_qam_canary(),
                (),
            )
        except LookupError:
            canary = MasterCaptureRetroQamCanaryV0_1(
                MasterCaptureSummaryState.UNAVAILABLE,
                None,
                ("retro-qam-canary-unavailable",),
            )
        return MasterCaptureSnapshotV0_1(
            1,
            query.start_utc_ns,
            query.stop_utc_ns,
            batches,
            next_cursor,
            MasterCaptureObservationV0_1(
                MasterCaptureSummaryState.COMPLETE, observation, ()
            ),
            canary,
            (
                "candidate-only-qam-goodness-not-starlink-detection",
                "radio-lnb-receiver-series-are-never-pooled",
            ),
        )


def _batches(
    rows: list[dict[str, object]],
    doppler: Mapping[RecordingId, MasterCaptureDopplerV0_1],
    qam: Mapping[RecordingId, MasterCaptureQamV0_1],
) -> tuple[MasterCaptureBatchV0_1, ...]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row["batch_id"]), []).append(row)
    result = []
    for batch_rows in grouped.values():
        first = batch_rows[0]
        attempt_rows: dict[str, list[dict[str, object]]] = {}
        for row in batch_rows:
            attempt_rows.setdefault(str(row["attempt_id"]), []).append(row)
        attempts = tuple(
            _attempt(values, doppler, qam)
            for values in sorted(
                attempt_rows.values(),
                key=lambda values: _integer(values[0]["attempt_position"]),
            )
        )
        if len(attempts) != 2:
            raise RuntimeError("master capture batch has incomplete attempts")
        result.append(
            MasterCaptureBatchV0_1(
                CaptureBatchId(str(first["batch_id"])),
                CaptureBatchMode(str(first["mode"])),
                CoordinationClaim(str(first["coordination_claim"])),
                attempts,
                _integer(first["capture_revision"]),
                _integer(first["requested_start_skew_ns"]),
                _optional_integer(first["observed_start_skew_ns"]),
                _optional_integer(first["maximum_observed_start_skew_ns"]),
                PairedAnalysisEligibility(str(first["paired_analysis_eligibility"])),
            )
        )
    return tuple(result)


def _attempt(
    rows: list[dict[str, object]],
    doppler: Mapping[RecordingId, MasterCaptureDopplerV0_1],
    qam: Mapping[RecordingId, MasterCaptureQamV0_1],
) -> MasterCaptureAttemptV0_1:
    first = rows[0]
    recording_id = _optional_recording_id(first["recording_id"])
    candidates = tuple(
        MasterCaptureQamCandidateV0_1(
            RecordingId(str(row["recording_id"])),
            RadioId(str(row["radio_id"])),
            str(row["qam_lnb_id"]),
            ReceiverChainId(str(row["qam_receiver_chain_id"])),
            SegmentId(str(row["qam_segment_id"])),
            StarlinkEdge(str(row["qam_edge"])),
            _number(row["qam_goodness"]),
            _number(row["qam_hard_symbol_accuracy"]),
            _number(row["qam_rms_evm"]),
            _integer(row["qam_window_count"]),
            str(row["qam_analysis_id"]),
        )
        for row in rows
        if row["qam_analysis_id"] is not None
    )
    analysis_state = DashboardAnalysisState(str(first["analysis_state"]))
    qam_state, qam_reasons = _qam_state(
        recording_id,
        analysis_state,
        candidates,
        _optional_integer(first["qam_assignment_count"]),
    )
    qam_view = (
        None if recording_id is None else qam.get(recording_id)
    ) or MasterCaptureQamV0_1(qam_state, candidates, qam_reasons)
    evidence_state, evidence_reasons = _unprojected_state(analysis_state)
    doppler_view = None if recording_id is None else doppler.get(recording_id)
    if doppler_view is None:
        doppler_view = MasterCaptureDopplerV0_1(
            evidence_state,
            (),
            _reason_for(
                evidence_state,
                evidence_reasons,
                "normalized-doppler-summary-unavailable",
            ),
        )
    return MasterCaptureAttemptV0_1(
        CaptureAttemptId(str(first["attempt_id"])),
        RadioId(str(first["radio_id"])),
        PlanId(str(first["plan_id"])),
        UtcNs(_integer(first["attempt_requested_start_utc_ns"])),
        DashboardCaptureState(str(first["capture_state"])),
        _optional_utc_ns(first["observed_start_utc_ns"]),
        recording_id,
        None if first["failure_reason"] is None else str(first["failure_reason"]),
        analysis_state,
        _boolean(first["analysis_result_available"]),
        None if recording_id is None else f"/recordings/{recording_id}",
        _optional_integer(first["capture_duration_ns"]),
        qam_view,
        doppler_view,
        MasterCapturePilotV0_1(
            evidence_state,
            None,
            None,
            _reason_for(
                evidence_state,
                evidence_reasons,
                "calibrated-pilot-count-unavailable",
            ),
        ),
        MasterCaptureSatelliteV0_1(
            MasterCaptureSummaryState.UNAVAILABLE,
            None,
            ("recording-satellite-association-unavailable",),
        ),
    )


def _qam_state(
    recording_id: RecordingId | None,
    analysis_state: DashboardAnalysisState,
    candidates: tuple[MasterCaptureQamCandidateV0_1, ...],
    assignment_count: int | None,
) -> tuple[MasterCaptureSummaryState, tuple[str, ...]]:
    # V22 proves positive candidate summaries only. Its absence must remain
    # conservative until the integration-owned receipt/work snapshot seam
    # supplies no_candidate, pending, or failed as an explicit stored state.
    if candidates:
        reasons: tuple[str, ...] = ()
        if assignment_count is not None and len(candidates) < assignment_count:
            reasons = ("some-authoritative-receivers-have-no-published-qam",)
        return MasterCaptureSummaryState.COMPLETE, reasons
    if recording_id is None:
        return MasterCaptureSummaryState.UNAVAILABLE, ("recording-unavailable",)
    if analysis_state in {
        DashboardAnalysisState.PENDING,
        DashboardAnalysisState.RUNNING,
    }:
        return MasterCaptureSummaryState.PENDING, ("qam-analysis-pending",)
    if analysis_state is DashboardAnalysisState.FAILED:
        return MasterCaptureSummaryState.FAILED, ("analysis-failed",)
    return MasterCaptureSummaryState.NOT_ANALYZED, (
        "published-qam-summary-unavailable",
    )


def _unprojected_state(
    analysis_state: DashboardAnalysisState,
) -> tuple[MasterCaptureSummaryState, tuple[str, ...]]:
    if analysis_state in {
        DashboardAnalysisState.PENDING,
        DashboardAnalysisState.RUNNING,
    }:
        return MasterCaptureSummaryState.PENDING, ("analysis-pending",)
    if analysis_state is DashboardAnalysisState.FAILED:
        return MasterCaptureSummaryState.FAILED, ("analysis-failed",)
    return MasterCaptureSummaryState.UNAVAILABLE, ()


def _reason_for(
    state: MasterCaptureSummaryState,
    inherited: tuple[str, ...],
    unavailable: str,
) -> tuple[str, ...]:
    return (
        inherited
        if state is not MasterCaptureSummaryState.UNAVAILABLE
        else (unavailable,)
    )


def _next_cursor(
    rows: list[dict[str, object]],
    batches: tuple[MasterCaptureBatchV0_1, ...],
    fingerprint: str,
) -> str | None:
    if not rows or not batches or not _boolean(rows[0]["has_more"]):
        return None
    last = batches[-1]
    requested = min(int(item.requested_start_utc_ns) for item in last.attempts)
    return _encode_cursor(
        fingerprint,
        _integer(rows[0]["snapshot_anchor"]),
        [requested, str(last.batch_id)],
    )


def _fingerprint(query: MasterCaptureSnapshotQueryV0_1) -> str:
    return (
        f"{int(query.start_utc_ns)}:{int(query.stop_utc_ns)}:{query.maximum_recordings}"
    )


def _encode_cursor(query: str, anchor: int, after: list[object]) -> str:
    payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "kind": "master_captures",
            "query": query,
            "anchor": anchor,
            "after": after,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str | None, query: str) -> dict[str, Any] | None:
    if cursor is None:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        state = json.loads(
            base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        )
        after = state["after"]
        if (
            set(state) != {"v", "kind", "query", "anchor", "after"}
            or state["v"] != _CURSOR_VERSION
            or state["kind"] != "master_captures"
            or state["query"] != query
            or isinstance(state["anchor"], bool)
            or not isinstance(state["anchor"], int)
            or not isinstance(after, list)
            or len(after) != 2
            or isinstance(after[0], bool)
            or not isinstance(after[0], int)
            or not isinstance(after[1], str)
        ):
            raise ValueError
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise InvalidCursor("cursor is invalid for this query") from error
    return cast(dict[str, Any], state)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("database integer is invalid")
    return value


def _optional_integer(value: object) -> int | None:
    return None if value is None else _integer(value)


def _optional_utc_ns(value: object) -> UtcNs | None:
    return None if value is None else UtcNs(_integer(value))


def _optional_recording_id(value: object) -> RecordingId | None:
    return None if value is None else RecordingId(str(value))


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("database boolean is invalid")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("database number is invalid")
    return float(value)
