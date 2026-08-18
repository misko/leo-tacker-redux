"""Contract-only projection of durable symbolwise replay for the dashboard."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Protocol

from leo_flow.contracts.dashboard_recording_evidence import (
    RecordingEvidenceContextQueryPortV0_1,
)
from leo_flow.contracts.dashboard_symbolwise_replay import (
    RecordingSymbolwiseReplayDashboardQueryV0_1,
    RecordingSymbolwiseReplayDashboardViewV0_1,
    SymbolwiseReplayDashboardStreamV0_1,
    SymbolwiseReplayPatternOverallV0_1,
    SymbolwiseReplayPatternPointV0_1,
    SymbolwiseReplayWindowPointV0_1,
)
from leo_flow.contracts.starlink_symbolwise_replay import (
    StarlinkSymbolwiseWindowEvidenceV0_1,
)
from leo_flow.contracts.starlink_symbolwise_replay_product import (
    MAXIMUM_RECORDING_REPLAY_QUERY_WINDOWS,
    RecordingStarlinkSymbolwiseReplayViewV0_1,
    StarlinkSymbolwiseReplayQueryV0_1,
    StarlinkSymbolwiseReplayStreamSelectionV0_1,
)

_SLICE_WINDOW_COUNT = 200
_OVERALL_DERIVATION = (
    "arithmetic-mean-and-maximum-selection-score-over-all-600-"
    "fixed-cadence-windows;ties-first-window"
)
_SUMMARY_DERIVATION = (
    "per-stream-per-pattern-only;arithmetic-mean-and-maximum-over-"
    "all-600-windows;no-cross-hardware-pooling"
)


class _DurableReplayQueryPort(Protocol):
    def recording_starlink_symbolwise_replay(
        self, query: StarlinkSymbolwiseReplayQueryV0_1
    ) -> RecordingStarlinkSymbolwiseReplayViewV0_1: ...


class RecordingSymbolwiseReplayDashboardProjectionV0_1:
    """Join only published contracts; storage identities never cross this seam."""

    def __init__(
        self,
        replay: _DurableReplayQueryPort,
        evidence_context: RecordingEvidenceContextQueryPortV0_1,
    ) -> None:
        self._replay = replay
        self._evidence_context = evidence_context

    def recording_symbolwise_replay_dashboard(
        self, query: RecordingSymbolwiseReplayDashboardQueryV0_1
    ) -> RecordingSymbolwiseReplayDashboardViewV0_1:
        context = self._evidence_context.recording_evidence_context(
            query.recording_id
        )
        assignments = {
            (item.radio_id, item.receiver_chain_id): item
            for item in context.receivers
            if item.recording_id == query.recording_id
            and (not query.radio_ids or item.radio_id in query.radio_ids)
            and (not query.lnb_ids or item.lnb_id in query.lnb_ids)
            and (
                not query.receiver_chain_ids
                or item.receiver_chain_id in query.receiver_chain_ids
            )
        }
        requested_receivers = tuple(
            sorted({key[1] for key in assignments})
        )
        explicitly_filtered = bool(
            query.radio_ids or query.lnb_ids or query.receiver_chain_ids
        )
        if explicitly_filtered and not assignments:
            return self._view(query, (), ())

        grouped: dict[
            tuple[str, str, str, str], list[StarlinkSymbolwiseWindowEvidenceV0_1]
        ] = defaultdict(list)
        selections: dict[
            tuple[str, str, str, str],
            StarlinkSymbolwiseReplayStreamSelectionV0_1,
        ] = {}
        reason_codes: tuple[str, ...] = ()
        for first in range(0, 600, _SLICE_WINDOW_COUNT):
            source = self._replay.recording_starlink_symbolwise_replay(
                StarlinkSymbolwiseReplayQueryV0_1(
                    recording_id=query.recording_id,
                    receiver_chain_ids=requested_receivers,
                    first_window_index=first,
                    stop_window_index=first + _SLICE_WINDOW_COUNT,
                    maximum_windows=MAXIMUM_RECORDING_REPLAY_QUERY_WINDOWS,
                )
            )
            if source.candidates_only is not True or source.truncated:
                raise RuntimeError("durable symbolwise replay slice is incomplete")
            reason_codes = source.reason_codes
            for stream in source.streams:
                selection = stream.selection
                assignment = assignments.get(
                    (selection.radio_id, selection.receiver_chain_id)
                )
                if assignment is None:
                    if explicitly_filtered:
                        continue
                    raise RuntimeError(
                        "symbolwise replay lacks authoritative LNB assignment"
                    )
                key = selection.identity
                selections[key] = selection
                grouped[key].extend(stream.windows)

        streams = tuple(
            self._stream(selections[key], assignments, tuple(grouped[key]))
            for key in sorted(grouped)
        )
        return self._view(query, streams, reason_codes)

    @staticmethod
    def _view(
        query: RecordingSymbolwiseReplayDashboardQueryV0_1,
        streams: tuple[SymbolwiseReplayDashboardStreamV0_1, ...],
        limitations: tuple[str, ...],
    ) -> RecordingSymbolwiseReplayDashboardViewV0_1:
        return RecordingSymbolwiseReplayDashboardViewV0_1(
            recording_id=query.recording_id,
            streams=streams,
            stream_count=len(streams),
            window_count_per_stream=600,
            point_count=len(streams) * 600,
            candidate_only=True,
            calibrated_detection_count=None,
            summary_derivation=_SUMMARY_DERIVATION,
            limitations=tuple(sorted(set(limitations))),
        )

    @staticmethod
    def _stream(selection, assignments, windows) -> SymbolwiseReplayDashboardStreamV0_1:
        if tuple(window.window_index for window in windows) != tuple(range(600)):
            raise RuntimeError("symbolwise replay did not provide all 600 windows")
        assignment = assignments[(selection.radio_id, selection.receiver_chain_id)]
        points = tuple(
            SymbolwiseReplayWindowPointV0_1(
                window_index=window.window_index,
                start_sample=window.start_sample,
                stop_sample=window.stop_sample,
                start_time_s=window.start_sample / selection.sample_rate_hz,
                stop_time_s=window.stop_sample / selection.sample_rate_hz,
                patterns=tuple(_pattern_point(item) for item in window.patterns),
            )
            for window in windows
        )
        overall = tuple(
            _overall(tuple(point.patterns[index] for point in points), points)
            for index in range(5)
        )
        return SymbolwiseReplayDashboardStreamV0_1(
            recording_id=assignment.recording_id,
            radio_id=selection.radio_id,
            lnb_id=assignment.lnb_id,
            receiver_chain_id=selection.receiver_chain_id,
            segment_id=selection.segment_id,
            edge=selection.edge,
            sample_rate_hz=selection.sample_rate_hz,
            frequency_center_cfo_hz=selection.frequency_center.center_cfo_hz,
            window_count=600,
            window_duration_ms=10,
            cadence_ms=100,
            analyzed_union_fraction=0.1,
            analyzed_union_percent=10.0,
            windows=points,
            overall=overall,
            candidates_only=True,
        )


def _pattern_point(item) -> SymbolwiseReplayPatternPointV0_1:
    role = item.pattern.role.value
    index = item.pattern.codebook_index
    label = (
        "Candidate-only · Qin exact"
        if index is None
        else f"Candidate-only · surrogate {index + 1}"
    )
    return SymbolwiseReplayPatternPointV0_1(
        pattern_id=item.pattern.pattern_id,
        pattern_role=role,
        codebook_index=index,
        candidate_label=label,
        selection_score=item.selection_score,
        winning_cfo_hz=item.winning_cfo_hz,
        winning_epoch_sample=item.winning_epoch_sample,
    )


def _overall(patterns, windows) -> SymbolwiseReplayPatternOverallV0_1:
    winner_index = max(
        range(len(patterns)), key=lambda index: patterns[index].selection_score
    )
    winner = patterns[winner_index]
    return SymbolwiseReplayPatternOverallV0_1(
        pattern_id=winner.pattern_id,
        pattern_role=winner.pattern_role,
        codebook_index=winner.codebook_index,
        candidate_label=winner.candidate_label,
        mean_selection_score=math.fsum(item.selection_score for item in patterns)
        / len(patterns),
        maximum_selection_score=winner.selection_score,
        winning_window_index=windows[winner_index].window_index,
        winning_window_start_time_s=windows[winner_index].start_time_s,
        winning_cfo_hz=winner.winning_cfo_hz,
        winning_epoch_sample=winner.winning_epoch_sample,
        derivation=_OVERALL_DERIVATION,
    )
