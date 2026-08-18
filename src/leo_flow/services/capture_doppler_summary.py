"""Bounded composition of master-table rows and published Doppler totals."""

from __future__ import annotations

from leo_flow.contracts.core import SegmentId
from leo_flow.contracts.dashboard_capture_doppler import (
    CaptureDopplerCandidateSummaryV0_1,
    CaptureDopplerRecordingSummaryV0_1,
    CaptureDopplerScopeQueryPortV0_1,
    CaptureDopplerState,
    CaptureDopplerSummaryQueryPortV0_1,
    CaptureDopplerSummaryQueryV0_1,
    CaptureDopplerSummaryViewV0_1,
)
from leo_flow.contracts.dashboard_doppler_aggregate import (
    DopplerAggregateQueryPortV0_1,
    DopplerAggregateQueryV0_1,
    DopplerAggregateSeriesV0_1,
)


class CaptureDopplerSummaryQueryServiceV0_1(CaptureDopplerSummaryQueryPortV0_1):
    def __init__(
        self,
        scopes: CaptureDopplerScopeQueryPortV0_1,
        aggregate: DopplerAggregateQueryPortV0_1,
    ) -> None:
        self._scopes = scopes
        self._aggregate = aggregate

    def capture_doppler_summaries(
        self, query: CaptureDopplerSummaryQueryV0_1
    ) -> CaptureDopplerSummaryViewV0_1:
        scope = self._scopes.capture_doppler_scope(query)
        aggregate = self._aggregate.doppler_aggregate(
            DopplerAggregateQueryV0_1(
                query.start_utc_ns,
                query.stop_utc_ns,
                methods=("basic",),
            )
        )
        by_recording: dict[str, list[DopplerAggregateSeriesV0_1]] = {}
        for aggregate_series in aggregate.series:
            if aggregate_series.method == "basic":
                by_recording.setdefault(aggregate_series.recording_id, []).append(
                    aggregate_series
                )
        recordings = []
        for scope_recording in scope.recordings:
            candidates = []
            aggregate_items = by_recording.get(str(scope_recording.recording_id), [])
            for assignment in scope_recording.assignments:
                matching = tuple(
                    value
                    for value in aggregate_items
                    if value.radio_id == str(scope_recording.radio_id)
                    and value.receiver_chain_id == str(assignment.receiver_chain_id)
                )
                if not matching:
                    continue
                selected = max(
                    matching,
                    key=lambda value: (
                        value.ranking_or_heldout_score,
                        value.candidate_or_path_id,
                    ),
                )
                candidates.append(
                    CaptureDopplerCandidateSummaryV0_1(
                        scope_recording.recording_id,
                        scope_recording.radio_id,
                        assignment.lnb_id,
                        assignment.receiver_chain_id,
                        SegmentId(selected.segment_id),
                        selected.candidate_or_path_id,
                        selected.model,
                        selected.drift_rate_hz_s,
                        selected.ranking_or_heldout_score,
                        selected.doppler_id,
                        selected.algorithm_version,
                    )
                )
            candidates.sort(key=lambda value: (value.lnb_id, value.receiver_chain_id))
            state, reasons = _state(
                scope_recording.analysis_state,
                bool(candidates),
                scope_recording.assignments,
            )
            if state is CaptureDopplerState.COMPLETE and len(candidates) < len(
                scope_recording.assignments
            ):
                reasons = ("some-authoritative-receivers-have-no-published-total",)
            recordings.append(
                CaptureDopplerRecordingSummaryV0_1(
                    scope_recording.recording_id,
                    scope_recording.radio_id,
                    scope_recording.analysis_state,
                    state,
                    tuple(candidates) if state is CaptureDopplerState.COMPLETE else (),
                    reasons,
                )
            )
        return CaptureDopplerSummaryViewV0_1(
            1,
            query.start_utc_ns,
            query.stop_utc_ns,
            True,
            None,
            tuple(recordings),
            scope.original_recording_count,
            scope.truncated,
            (
                "candidate-only-evidence-not-satellite-detection",
                "highest-score-selected-independently-per-authoritative-lnb-receiver",
                "radio-lnb-receiver-candidates-are-never-pooled",
            ),
        )


def _state(
    analysis_state: str, has_candidates: bool, assignments: tuple[object, ...]
) -> tuple[CaptureDopplerState, tuple[str, ...]]:
    if has_candidates:
        return CaptureDopplerState.COMPLETE, ()
    if not assignments:
        return CaptureDopplerState.UNAVAILABLE, ("hardware-assignment-unresolved",)
    if analysis_state in {"pending", "running"}:
        return CaptureDopplerState.PENDING, ("doppler-analysis-pending",)
    if analysis_state in {"failed", "error"}:
        return CaptureDopplerState.ERROR, ("analysis-failed",)
    return CaptureDopplerState.UNAVAILABLE, ("published-total-doppler-unavailable",)
