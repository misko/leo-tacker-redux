"""Exact pattern-symmetric refinement of complete-IQ prescreen seeds."""

from __future__ import annotations

from leo_flow.contracts.capture import SegmentManifest
from leo_flow.contracts.core import (
    Provenance,
    SchemaRef,
    SegmentId,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.starlink_adaptive_response import (
    StarlinkAdaptiveResponsePointV0_1,
)
from leo_flow.contracts.starlink_detector_suite import REPORT_METHOD_ORDER
from leo_flow.contracts.starlink_full_dwell_response import (
    StarlinkFullDwellSurrogateV0_1,
    StarlinkFullDwellWinnerV0_1,
)
from leo_flow.contracts.starlink_pilot_refinement import (
    StarlinkPilotRefinementBundleV0_1,
    StarlinkPilotRefinementRequestV0_1,
    StarlinkPilotRefinementStreamSelectionV0_1,
    StarlinkPilotRefinementStreamV0_1,
)
from leo_flow.contracts.starlink_surrogate_null import StarlinkPatternMethodEvidenceV0_1
from leo_flow.storage.ports import RecordingView

from .api import AnalysisExecutionContext
from .quality import decode_ci16
from .starlink_detector_suite import StarlinkDetectorSuiteConfigV0_2
from .starlink_surrogate_null import (
    ReportMethodStarlinkDetectorV0_1,
    StarlinkPairedSurrogateAnalyzerV0_1,
    radio_signal_v0_1,
    starlink_search_grid_v0_1,
)


class ExactStarlinkPilotRefinementAnalyzerV0_1:
    def __init__(
        self,
        config: StarlinkDetectorSuiteConfigV0_2,
        execution: AnalysisExecutionContext,
        paired: StarlinkPairedSurrogateAnalyzerV0_1 | None = None,
    ) -> None:
        self._config, self._execution = config, execution
        self._paired = paired or StarlinkPairedSurrogateAnalyzerV0_1(
            ReportMethodStarlinkDetectorV0_1(
                execution, condition_relative_on_acquire=True
            ),
            config,
        )

    def analyze(
        self, recording: RecordingView, request: StarlinkPilotRefinementRequestV0_1
    ) -> StarlinkPilotRefinementBundleV0_1:
        if (
            recording.manifest.recording_id != request.recording_id
            or request.search_grid != starlink_search_grid_v0_1(self._config)
        ):
            raise ValueError("pilot-refinement recording or search grid differs")
        segments = {
            segment.segment_id: segment for segment in recording.manifest.segments
        }
        streams = tuple(
            self._stream(recording, request, selection, segments)
            for selection in request.streams
        )
        recording_digest = request.recording_object_ref.identity_digest()
        provenance = Provenance(
            self._execution.producer_name,
            self._execution.producer_version,
            self._execution.git_commit,
            self._execution.environment_digest,
            canonical_digest((request.source_prescreen_ref, request.search_grid)),
            (
                recording_digest,
                request.source_prescreen_ref.digest,
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
        return StarlinkPilotRefinementBundleV0_1(
            SchemaRef(
                StarlinkPilotRefinementBundleV0_1.SCHEMA_ID, request.schema.version
            ),
            f"slpr_{token[:32]}",
            request.recording_id,
            recording_digest,
            request.source_prescreen_ref,
            request.source_suite_ref,
            request.digest,
            request.search_grid,
            streams,
            provenance,
            (
                "candidate-evidence-not-calibrated-detection",
                "complete-iq-selection-shared-by-qin-and-surrogates",
                "time-epoch-cfo-look-elsewhere-calibration-required",
                "stationary-ofdm-and-tones-may-enter-refinement",
            ),
            None,
        )

    def _stream(
        self,
        recording: RecordingView,
        request: StarlinkPilotRefinementRequestV0_1,
        selection: StarlinkPilotRefinementStreamSelectionV0_1,
        segments: dict[SegmentId, SegmentManifest],
    ) -> StarlinkPilotRefinementStreamV0_1:
        segment = segments.get(selection.segment_id)
        if segment is None or (
            segment.sample_count != selection.segment_sample_count
            or segment.actual_sample_rate_hz != selection.sample_rate_hz
            or recording.manifest.radio_id != selection.radio_id
        ):
            raise ValueError("pilot-refinement stream differs from manifest")
        try:
            receiver_index = segment.requested.receiver_chain_ids.index(
                selection.receiver_chain_id
            )
        except ValueError as error:
            raise ValueError("pilot-refinement receiver is unavailable") from error
        receiver_count = len(segment.requested.receiver_chain_ids)
        recording_digest = request.recording_object_ref.identity_digest()
        points = []
        for seed in selection.seeds:
            samples = _receiver_samples(
                recording,
                selection.segment_id,
                seed.start_sample,
                seed.stop_sample,
                receiver_count,
                receiver_index,
            )
            paired = self._paired.analyze(
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
            exact = {method.method: method for method in paired.exact.methods}
            controls = tuple(
                {method.method: method for method in surrogate.methods}
                for surrogate in paired.surrogates
            )
            utc_start = UtcNs(
                int(segment.start_utc_ns)
                + round(seed.start_sample * 1_000_000_000 / selection.sample_rate_hz)
            )
            utc_stop = UtcNs(
                int(segment.start_utc_ns)
                + round(seed.stop_sample * 1_000_000_000 / selection.sample_rate_hz)
            )
            for method in REPORT_METHOD_ORDER:
                qin = _winner(exact[method], seed.start_sample)
                surrogates = tuple(
                    StarlinkFullDwellSurrogateV0_1(
                        index,
                        paired.surrogates[index].pattern.template_ref.digest,
                        _winner(control[method], seed.start_sample),
                    )
                    for index, control in enumerate(controls)
                )
                scores = tuple(item.winner.score for item in surrogates)
                points.append(
                    StarlinkAdaptiveResponsePointV0_1(
                        method,
                        seed.seed_index,
                        seed.start_sample,
                        seed.stop_sample,
                        utc_start,
                        utc_stop,
                        qin,
                        surrogates,
                        1 + sum(score >= qin.score for score in scores),
                        qin.score - max(scores),
                    )
                )
        covered = _union_sample_count(
            tuple((seed.start_sample, seed.stop_sample) for seed in selection.seeds)
        )
        return StarlinkPilotRefinementStreamV0_1(
            selection,
            tuple(points),
            covered,
            covered / selection.segment_sample_count,
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
        raise ValueError("pilot-refinement reader returned another interval")
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
