"""Normalized, candidate-only QAM rows written beside durable QAM products."""

from __future__ import annotations

from typing import Any, cast

import psycopg
from psycopg.types.json import Jsonb

from leo_flow.analysis.qam_goodness import qam_goodness_v0_2
from leo_flow.contracts.dashboard_qam_summary_receipt import (
    DASHBOARD_QAM_SUMMARY_CONFIG_REF_V0_2,
    QamSummaryTerminalOutcome,
    dashboard_qam_candidate_set_digest_v0_2,
)
from leo_flow.contracts.starlink_acquired_constellation_pipeline import (
    StarlinkAcquiredConstellationRecordingBundleV0_3,
)
from leo_flow.contracts.starlink_adaptive_qam import StarlinkAdaptiveQamBundleV0_4


def publish_acquired_qam_summary_with_cursor(
    cursor: psycopg.Cursor[Any],
    bundle: StarlinkAcquiredConstellationRecordingBundleV0_3,
) -> None:
    lnb_by_receiver = {
        str(row["receiver_chain_id"]): str(row["lnb_id"])
        for row in cursor.execute(
            "SELECT c.receiver_chain_id,c.lnb_id FROM public.recording_hardware_link l "
            "JOIN public.hardware_receiver_chain c ON c.snapshot_id=l.hardware_snapshot_id "
            "WHERE l.recording_id=%s",
            (str(bundle.recording_id),),
        ).fetchall()
    }
    rows = []
    for stream in bundle.streams:
        lnb_id = lnb_by_receiver.get(str(stream.receiver_chain_id))
        if lnb_id is None:
            continue
        best = max(
            stream.windows,
            key=lambda item: (
                qam_goodness_v0_2(
                    item.evidence.hard_symbol_accuracy, item.evidence.rms_evm
                ),
                item.evidence.verify_minus_control_margin,
                -item.window_index,
            ),
        )
        rows.append(
            _row(
                "acquired-v0.3",
                bundle.analysis_id,
                bundle.recording_id,
                stream.radio_id,
                lnb_id,
                stream.receiver_chain_id,
                stream.segment_id,
                stream.edge.value,
                best.evidence.hard_symbol_accuracy,
                best.evidence.rms_evm,
                stream.overall.window_count,
            )
        )
    _publish(cursor, "acquired-v0.3", bundle.analysis_id, _best_per_receiver(rows))


def publish_adaptive_qam_summary_with_cursor(
    cursor: psycopg.Cursor[Any], bundle: StarlinkAdaptiveQamBundleV0_4
) -> None:
    rows = []
    for selected, stream in zip(
        bundle.stream_selections, bundle.evidence_bundle.streams, strict=True
    ):
        best = max(
            stream.windows,
            key=lambda item: (
                qam_goodness_v0_2(
                    item.evidence.hard_symbol_accuracy, item.evidence.rms_evm
                ),
                item.evidence.verify_minus_control_margin,
                -item.window_index,
            ),
        )
        rows.append(
            _row(
                "adaptive-v0.4",
                bundle.analysis_id,
                bundle.recording_id,
                selected.radio_id,
                selected.lnb_id,
                selected.receiver_chain_id,
                selected.segment_id,
                selected.edge.value,
                best.evidence.hard_symbol_accuracy,
                best.evidence.rms_evm,
                stream.overall.window_count,
            )
        )
    _publish(cursor, "adaptive-v0.4", bundle.analysis_id, _best_per_receiver(rows))


def _row(
    kind: str,
    analysis_id: str,
    recording_id: object,
    radio_id: object,
    lnb_id: str,
    receiver_chain_id: object,
    segment_id: object,
    edge: str,
    accuracy: float,
    evm: float,
    window_count: int,
) -> dict[str, object]:
    return {
        "source_kind": kind,
        "analysis_id": analysis_id,
        "recording_id": str(recording_id),
        "radio_id": str(radio_id),
        "lnb_id": lnb_id,
        "receiver_chain_id": str(receiver_chain_id),
        "segment_id": str(segment_id),
        "edge": edge,
        "qam_goodness": qam_goodness_v0_2(accuracy, evm),
        "hard_symbol_accuracy": accuracy,
        "rms_evm": evm,
        "window_count": window_count,
    }


def _best_per_receiver(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    selected: dict[tuple[str, str, str], dict[str, object]] = {}
    for row in rows:
        key = (str(row["radio_id"]), str(row["lnb_id"]), str(row["receiver_chain_id"]))
        current = selected.get(key)
        if current is None or (
            float(cast(Any, row["qam_goodness"])),
            str(row["segment_id"]),
            str(row["edge"]),
        ) > (
            float(cast(Any, current["qam_goodness"])),
            str(current["segment_id"]),
            str(current["edge"]),
        ):
            selected[key] = row
    return [selected[key] for key in sorted(selected)]


def _publish(
    cursor: psycopg.Cursor[Any],
    kind: str,
    analysis_id: str,
    rows: list[dict[str, object]],
) -> None:
    outcome = (
        QamSummaryTerminalOutcome.COMPLETE
        if rows
        else QamSummaryTerminalOutcome.NO_CANDIDATE
    )
    candidate_set_digest = dashboard_qam_candidate_set_digest_v0_2(rows)
    result = cursor.execute(
        "SELECT public.publish_dashboard_capture_qam_summary_receipt_v0_2(%s,%s,%s,%s,%s,%s) AS published",
        (
            kind,
            analysis_id,
            DASHBOARD_QAM_SUMMARY_CONFIG_REF_V0_2.digest.value,
            candidate_set_digest.value,
            outcome.value,
            Jsonb(rows),
        ),
    ).fetchone()
    if result is None or result["published"] is not True:
        raise RuntimeError("QAM summary projection was not acknowledged")
