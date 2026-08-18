"""Single-query PostgreSQL projection for master-table QAM summaries."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row

from leo_flow.contracts.core import RadioId, ReceiverChainId, RecordingId, SegmentId
from leo_flow.contracts.dashboard_capture_qam import (
    CaptureQamCandidateSummaryV0_1,
    CaptureQamRecordingSummaryV0_1,
    CaptureQamState,
    CaptureQamSummaryQueryV0_1,
    CaptureQamSummaryViewV0_1,
)
from leo_flow.contracts.starlink import StarlinkEdge

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresCaptureQamSummaryRepositoryV0_1:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def capture_qam_summaries(
        self, query: CaptureQamSummaryQueryV0_1
    ) -> CaptureQamSummaryViewV0_1:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            rows = cursor.execute(
                "SELECT * FROM public.read_dashboard_capture_qam_summaries_v0_1(%s,%s,%s)",
                (
                    int(query.start_utc_ns),
                    int(query.stop_utc_ns),
                    query.maximum_recordings,
                ),
            ).fetchall()
        grouped: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            grouped.setdefault(str(row["recording_id"]), []).append(row)
        summaries = []
        for recording_id, items in grouped.items():
            first = items[0]
            candidates = tuple(
                CaptureQamCandidateSummaryV0_1(
                    RecordingId(recording_id),
                    RadioId(str(first["radio_id"])),
                    str(item["lnb_id"]),
                    ReceiverChainId(str(item["receiver_chain_id"])),
                    SegmentId(str(item["segment_id"])),
                    StarlinkEdge(str(item["edge"])),
                    float(cast(Any, item["qam_goodness"])),
                    float(cast(Any, item["hard_symbol_accuracy"])),
                    float(cast(Any, item["rms_evm"])),
                    int(cast(Any, item["window_count"])),
                    str(item["analysis_id"]),
                )
                for item in items
                if item["analysis_id"] is not None
            )
            assignment_count = int(cast(Any, first["assignment_count"]))
            analysis_state = str(first["analysis_state"])
            if candidates:
                state = CaptureQamState.COMPLETE
                reasons = (
                    ()
                    if len(candidates) == assignment_count
                    else ("some-authoritative-receivers-have-no-published-qam",)
                )
            elif assignment_count == 0:
                state, reasons = (
                    CaptureQamState.UNAVAILABLE,
                    ("hardware-assignment-unresolved",),
                )
            elif analysis_state in {"pending", "running"}:
                state, reasons = (
                    CaptureQamState.PENDING,
                    ("acquired-qam-analysis-pending",),
                )
            elif analysis_state in {"failed", "error"}:
                state, reasons = CaptureQamState.ERROR, ("analysis-failed",)
            else:
                state, reasons = (
                    CaptureQamState.UNAVAILABLE,
                    ("published-acquired-qam-summary-unavailable",),
                )
            summaries.append(
                CaptureQamRecordingSummaryV0_1(
                    RecordingId(recording_id),
                    RadioId(str(first["radio_id"])),
                    analysis_state,
                    state,
                    candidates,
                    reasons,
                )
            )
        original = int(cast(Any, rows[0]["original_recording_count"])) if rows else 0
        return CaptureQamSummaryViewV0_1(
            1,
            query.start_utc_ns,
            query.stop_utc_ns,
            True,
            True,
            None,
            tuple(summaries),
            original,
            original > len(summaries),
            (
                "candidate-only-qam-goodness-not-starlink-detection",
                "highest-goodness-selected-independently-per-authoritative-lnb-receiver",
                "best-analyzed-window-not-support-weighted-dwell-mean",
                "radio-lnb-receiver-series-are-never-pooled",
            ),
        )
