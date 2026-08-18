"""Bounded composition of master-table rows and durable acquired-QAM evidence."""

from __future__ import annotations

from dataclasses import dataclass

from leo_flow.analysis.qam_goodness import qam_goodness_v0_2
from leo_flow.contracts.core import RadioId, ReceiverChainId, SegmentId
from leo_flow.contracts.dashboard_capture_doppler import (
    CaptureDopplerScopeQueryPortV0_1,
    CaptureDopplerSummaryQueryV0_1,
)
from leo_flow.contracts.dashboard_capture_qam import (
    CaptureQamCandidateSummaryV0_1,
    CaptureQamRecordingSummaryV0_1,
    CaptureQamState,
    CaptureQamSummaryQueryPortV0_1,
    CaptureQamSummaryQueryV0_1,
    CaptureQamSummaryViewV0_1,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_acquired_constellation_pipeline import (
    MAX_ACQUIRED_QAM_QUERY_STREAMS,
    RecordingStarlinkAcquiredConstellationQueryPortV0_3,
    RecordingStarlinkAcquiredConstellationViewV0_3,
    StarlinkAcquiredConstellationPresentationWindowV0_3,
    StarlinkAcquiredConstellationQueryV0_3,
    StarlinkAcquiredConstellationViewMode,
)
from leo_flow.contracts.starlink_adaptive_qam import (
    RecordingStarlinkAdaptiveQamQueryPortV0_4,
    RecordingStarlinkAdaptiveQamViewV0_4,
)


class CaptureQamSummaryQueryServiceV0_1(CaptureQamSummaryQueryPortV0_1):
    def __init__(
        self,
        scopes: CaptureDopplerScopeQueryPortV0_1,
        acquired_qam: RecordingStarlinkAcquiredConstellationQueryPortV0_3,
        adaptive_qam: RecordingStarlinkAdaptiveQamQueryPortV0_4 | None = None,
    ) -> None:
        self._scopes, self._acquired_qam = scopes, acquired_qam
        self._adaptive_qam = adaptive_qam

    def capture_qam_summaries(
        self, query: CaptureQamSummaryQueryV0_1
    ) -> CaptureQamSummaryViewV0_1:
        scope = self._scopes.capture_doppler_scope(
            CaptureDopplerSummaryQueryV0_1(
                query.start_utc_ns, query.stop_utc_ns, query.maximum_recordings
            )
        )
        recordings = []
        for scoped in scope.recordings:
            candidates: tuple[CaptureQamCandidateSummaryV0_1, ...] = ()
            reasons: tuple[str, ...] = ()
            qam_query = StarlinkAcquiredConstellationQueryV0_3(
                scoped.recording_id,
                StarlinkAcquiredConstellationViewMode.WINDOWS,
                maximum_streams=MAX_ACQUIRED_QAM_QUERY_STREAMS,
                maximum_windows_per_stream=32,
                maximum_points_per_constellation=1,
            )
            try:
                view = self._qam_view(qam_query)
            except LookupError:
                state, reasons = _missing_state(
                    scoped.analysis_state, bool(scoped.assignments)
                )
            else:
                assignments = {
                    item.receiver_chain_id: item.lnb_id for item in scoped.assignments
                }
                selected: dict[tuple[str, str], CaptureQamCandidateSummaryV0_1] = {}
                for stream in _summary_streams(view):
                    expected_lnb = assignments.get(stream.receiver_chain_id)
                    if (
                        stream.radio_id != scoped.radio_id
                        or expected_lnb is None
                        or stream.lnb_id != expected_lnb
                    ):
                        raise RuntimeError("acquired-QAM stream scope is inconsistent")
                    windows = stream.windows
                    if not windows:
                        continue
                    best = max(
                        windows,
                        key=lambda window: (
                            qam_goodness_v0_2(
                                window.hard_symbol_accuracy, window.rms_evm
                            ),
                            window.verify_minus_control_margin,
                            -window.window_index,
                        ),
                    )
                    goodness = qam_goodness_v0_2(
                        best.hard_symbol_accuracy, best.rms_evm
                    )
                    candidate = CaptureQamCandidateSummaryV0_1(
                        scoped.recording_id,
                        scoped.radio_id,
                        stream.lnb_id,
                        stream.receiver_chain_id,
                        stream.segment_id,
                        stream.edge,
                        goodness,
                        best.hard_symbol_accuracy,
                        best.rms_evm,
                        stream.window_count,
                        view.analysis_ref.artifact_id,
                    )
                    key = (stream.lnb_id, str(stream.receiver_chain_id))
                    current = selected.get(key)
                    if current is None or (
                        candidate.qam_goodness,
                        str(candidate.segment_id),
                        candidate.edge.value,
                    ) > (
                        current.qam_goodness,
                        str(current.segment_id),
                        current.edge.value,
                    ):
                        selected[key] = candidate
                candidates = tuple(selected[key] for key in sorted(selected))
                state = (
                    CaptureQamState.COMPLETE
                    if candidates
                    else CaptureQamState.UNAVAILABLE
                )
                if not candidates:
                    reasons = ("published-acquired-qam-has-no-matching-streams",)
                elif len(candidates) < len(scoped.assignments):
                    reasons = ("some-authoritative-receivers-have-no-published-qam",)
            recordings.append(
                CaptureQamRecordingSummaryV0_1(
                    scoped.recording_id,
                    scoped.radio_id,
                    scoped.analysis_state,
                    state,
                    candidates if state is CaptureQamState.COMPLETE else (),
                    reasons,
                )
            )
        return CaptureQamSummaryViewV0_1(
            1,
            query.start_utc_ns,
            query.stop_utc_ns,
            True,
            True,
            None,
            tuple(recordings),
            scope.original_recording_count,
            scope.truncated,
            (
                "candidate-only-qam-goodness-not-starlink-detection",
                "highest-goodness-selected-independently-per-authoritative-lnb-receiver",
                "best-analyzed-window-not-support-weighted-dwell-mean",
                "radio-lnb-receiver-series-are-never-pooled",
            ),
        )

    def _qam_view(
        self, query: StarlinkAcquiredConstellationQueryV0_3
    ) -> (
        RecordingStarlinkAdaptiveQamViewV0_4
        | RecordingStarlinkAcquiredConstellationViewV0_3
    ):
        if self._adaptive_qam is not None:
            try:
                return self._adaptive_qam.recording_starlink_adaptive_qam(query)
            except LookupError:
                pass
        return self._acquired_qam.recording_starlink_acquired_constellation(query)


def _missing_state(
    analysis_state: str, has_assignments: bool
) -> tuple[CaptureQamState, tuple[str, ...]]:
    if not has_assignments:
        return CaptureQamState.UNAVAILABLE, ("hardware-assignment-unresolved",)
    if analysis_state in {"pending", "running"}:
        return CaptureQamState.PENDING, ("acquired-qam-analysis-pending",)
    if analysis_state in {"failed", "error"}:
        return CaptureQamState.ERROR, ("analysis-failed",)
    return CaptureQamState.UNAVAILABLE, ("published-acquired-qam-unavailable",)


@dataclass(frozen=True)
class _SummaryStream:
    radio_id: RadioId
    lnb_id: str
    receiver_chain_id: ReceiverChainId
    segment_id: SegmentId
    edge: StarlinkEdge
    windows: tuple[StarlinkAcquiredConstellationPresentationWindowV0_3, ...]
    window_count: int


def _summary_streams(
    view: (
        RecordingStarlinkAdaptiveQamViewV0_4
        | RecordingStarlinkAcquiredConstellationViewV0_3
    ),
) -> tuple[_SummaryStream, ...]:
    if isinstance(view, RecordingStarlinkAdaptiveQamViewV0_4):
        return tuple(
            _SummaryStream(
                stream.radio_id,
                stream.lnb_id,
                stream.receiver_chain_id,
                stream.segment_id,
                stream.edge,
                tuple(item.qam for item in stream.windows),
                stream.overall.window_count,
            )
            for stream in view.streams
        )
    return tuple(
        _SummaryStream(
            stream.radio_id,
            stream.lnb_id,
            stream.receiver_chain_id,
            stream.segment_id,
            stream.edge,
            stream.windows,
            stream.overall.window_count,
        )
        for stream in view.streams
    )
