"""Native bounded execution of adaptive Qin/surrogate response windows."""

from __future__ import annotations

from leo_flow.contracts.capture import SegmentManifest
from leo_flow.contracts.core import (
    Digest,
    Provenance,
    SchemaRef,
    SegmentId,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.starlink_adaptive_refinement import (
    StarlinkAdaptivePatternScoreV0_1,
)
from leo_flow.contracts.starlink_adaptive_response import (
    V0_1,
    StarlinkAdaptiveResponseBundleV0_1,
    StarlinkAdaptiveResponsePointV0_1,
    StarlinkAdaptiveResponseRequestV0_1,
    StarlinkAdaptiveResponseStreamV0_1,
    StarlinkAdaptiveStreamSelectionV0_1,
)
from leo_flow.contracts.starlink_detector_suite import (
    REPORT_METHOD_ORDER,
    StarlinkDetectorMethod,
)
from leo_flow.contracts.starlink_full_dwell_response import (
    StarlinkFullDwellSurrogateV0_1,
    StarlinkFullDwellWinnerV0_1,
)
from leo_flow.contracts.starlink_surrogate_null import (
    StarlinkPairedSurrogateEvidenceV0_1,
    StarlinkPatternMethodEvidenceV0_1,
)
from leo_flow.storage.ports import RecordingView

from .api import AnalysisExecutionContext
from .quality import decode_ci16
from .starlink_adaptive_refinement import (
    adaptive_base_windows_v0_1,
    adaptive_refinement_selection_v0_1,
)
from .starlink_detector_suite import StarlinkDetectorSuiteConfigV0_2
from .starlink_surrogate_null import (
    ReportMethodStarlinkDetectorV0_1,
    StarlinkPairedSurrogateAnalyzerV0_1,
    radio_signal_v0_1,
    starlink_search_grid_v0_1,
)


class ExactStarlinkAdaptiveResponseAnalyzerV0_1:
    def __init__(
        self,
        config: StarlinkDetectorSuiteConfigV0_2,
        execution: AnalysisExecutionContext,
        paired_analyzer: StarlinkPairedSurrogateAnalyzerV0_1 | None = None,
    ) -> None:
        self._config = config
        self._execution = execution
        self._paired = paired_analyzer or StarlinkPairedSurrogateAnalyzerV0_1(
            ReportMethodStarlinkDetectorV0_1(
                execution, condition_relative_on_acquire=True
            ),
            config,
        )

    def analyze(
        self, recording: RecordingView, request: StarlinkAdaptiveResponseRequestV0_1
    ) -> StarlinkAdaptiveResponseBundleV0_1:
        if recording.manifest.recording_id != request.recording_id:
            raise ValueError("recording and adaptive request identities differ")
        if request.search_grid != starlink_search_grid_v0_1(self._config):
            raise ValueError("adaptive request selects another search grid")
        segments = {item.segment_id: item for item in recording.manifest.segments}
        streams = tuple(
            sorted(
                (
                    self._stream(recording, request, selection, segments)
                    for selection in request.streams
                ),
                key=lambda item: item.identity,
            )
        )
        recording_digest = request.recording_object_ref.identity_digest()
        provenance = Provenance(
            self._execution.producer_name,
            self._execution.producer_version,
            self._execution.git_commit,
            self._execution.environment_digest,
            canonical_digest((request.plan, request.search_grid)),
            (
                recording_digest,
                request.timeline_ref.digest,
                request.source_suite_ref.digest,
            ),
            (request.search_grid.digest,),
            self._execution.started_utc_ns,
            self._execution.completed_utc_ns,
            self._execution.host_class,
        )
        token = canonical_digest(
            {
                "request": request.digest,
                "streams": tuple(canonical_digest(item) for item in streams),
            }
        ).value
        return StarlinkAdaptiveResponseBundleV0_1(
            SchemaRef(StarlinkAdaptiveResponseBundleV0_1.SCHEMA_ID, V0_1),
            f"slar_{token[:32]}",
            request.recording_id,
            recording_digest,
            request.timeline_ref,
            request.source_suite_ref,
            request.digest,
            request.search_grid,
            request.plan,
            streams,
            provenance,
            (
                "candidate-evidence-not-calibrated-detection",
                "finite-surrogate-rank-not-p-value",
                "time-look-elsewhere-calibration-required",
                "base-sentinels-span-dwell-but-do-not-cover-every-sample",
                "all-patterns-search-the-union-of-selected-local-windows",
                "report-methods-conditioned-on-each-patterns-full-frame-acquire-winner",
                "exact-window-union-is-sparse-and-dependent",
            ),
            None,
        )

    def _stream(
        self,
        recording: RecordingView,
        request: StarlinkAdaptiveResponseRequestV0_1,
        selection: StarlinkAdaptiveStreamSelectionV0_1,
        segments: dict[SegmentId, SegmentManifest],
    ) -> StarlinkAdaptiveResponseStreamV0_1:
        segment = segments.get(selection.segment_id)
        if segment is None:
            raise ValueError("adaptive selected segment is unavailable")
        if (
            segment.sample_count != selection.segment_sample_count
            or segment.actual_sample_rate_hz != selection.sample_rate_hz
            or recording.manifest.radio_id != selection.radio_id
        ):
            raise ValueError("adaptive stream differs from manifest")
        try:
            receiver_index = segment.requested.receiver_chain_ids.index(
                selection.receiver_chain_id
            )
        except ValueError as error:
            raise ValueError("adaptive selected receiver is unavailable") from error
        receiver_count = len(segment.requested.receiver_chain_ids)
        base = adaptive_base_windows_v0_1(
            selection.segment_sample_count,
            request.plan,
            tuple(
                (item.rank, item.start_sample, item.stop_sample)
                for item in selection.power_seeds
            ),
        )
        recording_digest = request.recording_object_ref.identity_digest()
        cache: dict[int, StarlinkPairedSurrogateEvidenceV0_1] = {}
        pattern_scores = []
        for item in base:
            paired = self._analyze_window(
                recording,
                request,
                selection,
                receiver_count,
                receiver_index,
                item.start_sample,
                recording_digest,
            )
            cache[item.start_sample] = paired
            base_exact = next(
                method
                for method in paired.exact.methods
                if method.method is StarlinkDetectorMethod.FULL_FRAME_ACQUIRE
            )
            pattern_scores.append(
                StarlinkAdaptivePatternScoreV0_1(
                    paired.exact.pattern.template_ref,
                    item.start_sample,
                    base_exact.score,
                )
            )
            for surrogate in paired.surrogates:
                base_method = next(
                    candidate
                    for candidate in surrogate.methods
                    if candidate.method is StarlinkDetectorMethod.FULL_FRAME_ACQUIRE
                )
                pattern_scores.append(
                    StarlinkAdaptivePatternScoreV0_1(
                        surrogate.pattern.template_ref,
                        item.start_sample,
                        base_method.score,
                    )
                )
        refined = adaptive_refinement_selection_v0_1(
            selection.segment_sample_count, request.plan, base, tuple(pattern_scores)
        )
        points = []
        for window in refined.exact_windows:
            cached = cache.get(window.start_sample)
            if cached is None:
                paired_window = self._analyze_window(
                    recording,
                    request,
                    selection,
                    receiver_count,
                    receiver_index,
                    window.start_sample,
                    recording_digest,
                )
            else:
                paired_window = cached
            exact = {item.method: item for item in paired_window.exact.methods}
            controls = tuple(
                {item.method: item for item in surrogate.methods}
                for surrogate in paired_window.surrogates
            )
            utc_start = UtcNs(
                int(segment.start_utc_ns)
                + round(window.start_sample * 1_000_000_000 / selection.sample_rate_hz)
            )
            utc_stop = UtcNs(
                int(segment.start_utc_ns)
                + round(window.stop_sample * 1_000_000_000 / selection.sample_rate_hz)
            )
            for detector_method in REPORT_METHOD_ORDER:
                qin = _winner(exact[detector_method], window.start_sample)
                surrogates = tuple(
                    StarlinkFullDwellSurrogateV0_1(
                        index,
                        paired_window.surrogates[index].pattern.template_ref.digest,
                        _winner(control[detector_method], window.start_sample),
                    )
                    for index, control in enumerate(controls)
                )
                scores = tuple(item.winner.score for item in surrogates)
                points.append(
                    StarlinkAdaptiveResponsePointV0_1(
                        detector_method,
                        window.window_index,
                        window.start_sample,
                        window.stop_sample,
                        utc_start,
                        utc_stop,
                        qin,
                        surrogates,
                        1 + sum(score >= qin.score for score in scores),
                        qin.score - max(scores),
                    )
                )
        covered = _union_sample_count(
            tuple(
                (item.start_sample, item.stop_sample) for item in refined.exact_windows
            )
        )
        return StarlinkAdaptiveResponseStreamV0_1(
            selection.radio_id,
            selection.lnb_id,
            selection.segment_id,
            selection.receiver_chain_id,
            selection.channel_number,
            selection.edge,
            selection.sample_rate_hz,
            selection.segment_sample_count,
            refined,
            tuple(points),
            covered,
            covered / selection.segment_sample_count,
        )

    def _analyze_window(
        self,
        recording: RecordingView,
        request: StarlinkAdaptiveResponseRequestV0_1,
        selection: StarlinkAdaptiveStreamSelectionV0_1,
        receiver_count: int,
        receiver_index: int,
        start: int,
        recording_digest: Digest,
    ) -> StarlinkPairedSurrogateEvidenceV0_1:
        samples = _receiver_samples(
            recording,
            selection.segment_id,
            start,
            start + request.plan.probe_sample_count,
            receiver_count,
            receiver_index,
        )
        return self._paired.analyze(
            radio_signal_v0_1(
                samples,
                recording_id=request.recording_id,
                recording_identity_digest=recording_digest,
                segment_id=selection.segment_id,
                receiver_chain_id=selection.receiver_chain_id,
                edge=selection.edge,
                sample_rate_hz=selection.sample_rate_hz,
            ),
            surrogate_count=request.surrogate_count,
        )


def _receiver_samples(
    recording: RecordingView,
    segment_id: SegmentId,
    start: int,
    stop: int,
    receiver_count: int,
    receiver_index: int,
) -> tuple[complex, ...]:
    values, count = decode_ci16(
        recording.read_iq_bytes(segment_id, start, stop), receiver_count
    )
    if count != stop - start:
        raise ValueError("adaptive response reader returned another interval")
    stride = receiver_count * 2
    offset = receiver_index * 2
    return tuple(
        complex(values[position], values[position + 1])
        for position in range(offset, count * stride, stride)
    )


def _winner(
    method: StarlinkPatternMethodEvidenceV0_1, start: int
) -> StarlinkFullDwellWinnerV0_1:
    return StarlinkFullDwellWinnerV0_1(
        method.score,
        method.winning_epoch_sample,
        start + method.winning_epoch_sample,
        method.winning_coarse_cfo_hz,
        method.winning_residual_cfo_hz,
        method.effective_search_cell_count,
        method.search_mode,
    )


def _union_sample_count(intervals: tuple[tuple[int, int], ...]) -> int:
    if not intervals:
        return 0
    ordered = sorted(intervals)
    total = 0
    start, stop = ordered[0]
    for next_start, next_stop in ordered[1:]:
        if next_start <= stop:
            stop = max(stop, next_stop)
        else:
            total += stop - start
            start, stop = next_start, next_stop
    return total + stop - start
