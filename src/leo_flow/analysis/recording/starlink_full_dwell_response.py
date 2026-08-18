"""Native bounded full-dwell Qin/surrogate window response analysis."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from typing import Protocol

from leo_flow.contracts.capture import SegmentManifest
from leo_flow.contracts.core import (
    Digest,
    Provenance,
    SchemaRef,
    SegmentId,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.starlink_detector_suite import REPORT_METHOD_ORDER
from leo_flow.contracts.starlink_full_dwell_response import (
    MAXIMUM_EXACT_WINDOWS_PER_STREAM,
    V0_1,
    StarlinkFullDwellPointV0_1,
    StarlinkFullDwellPrescreenWindowV0_1,
    StarlinkFullDwellRequestV0_1,
    StarlinkFullDwellResponseBundleV0_1,
    StarlinkFullDwellStreamResponseV0_1,
    StarlinkFullDwellStreamSelectionV0_1,
    StarlinkFullDwellSurrogateV0_1,
    StarlinkFullDwellWinnerV0_1,
    StarlinkWindowTier,
)
from leo_flow.contracts.starlink_surrogate_null import (
    StarlinkPairedSurrogateEvidenceV0_1,
    StarlinkPatternMethodEvidenceV0_1,
)
from leo_flow.storage.ports import RecordingView

from .api import AnalysisExecutionContext
from .quality import decode_ci16
from .starlink_detector_suite import StarlinkDetectorSuiteConfigV0_2
from .starlink_surrogate_null import (
    ReportMethodStarlinkDetectorV0_1,
    StarlinkPairedSurrogateAnalyzerV0_1,
    StarlinkRadioSignalV0_1,
    radio_signal_v0_1,
    starlink_search_grid_v0_1,
)


class _PairedAnalyzer(Protocol):
    def analyze(
        self, radio_signal: StarlinkRadioSignalV0_1, *, surrogate_count: int
    ) -> StarlinkPairedSurrogateEvidenceV0_1: ...


def covering_window_starts_v0_1(
    sample_count: int, window_sample_count: int, stride_samples: int
) -> tuple[int, ...]:
    """Return endpoint-preserving starts whose interval union is the full dwell."""
    if min(sample_count, window_sample_count, stride_samples) <= 0:
        raise ValueError("window dimensions must be positive")
    if stride_samples > window_sample_count:
        raise ValueError("covering window stride cannot leave gaps")
    if sample_count < window_sample_count:
        return ()
    last = sample_count - window_sample_count
    starts = list(range(0, last + 1, stride_samples))
    if starts[-1] != last:
        starts.append(last)
    return tuple(starts)


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
        raise ValueError("full-dwell reader returned another interval")
    stride = receiver_count * 2
    offset = receiver_index * 2
    return tuple(
        complex(values[position], values[position + 1])
        for position in range(offset, count * stride, stride)
    )


def mean_complex_power_v0_1(samples: Sequence[complex]) -> float:
    if not samples:
        raise ValueError("cannot prescreen an empty window")
    score = math.fsum(abs(value) ** 2 for value in samples) / len(samples)
    if not math.isfinite(score):
        raise ValueError("prescreen power must be finite")
    return score


class ExactStarlinkFullDwellResponseAnalyzerV0_1:
    """Analyze exact coarse cover windows and pattern-blind fine refinements."""

    def __init__(
        self,
        config: StarlinkDetectorSuiteConfigV0_2,
        execution: AnalysisExecutionContext,
        paired_analyzer: _PairedAnalyzer | None = None,
    ) -> None:
        self._config = config
        self._execution = execution
        self._paired = paired_analyzer or StarlinkPairedSurrogateAnalyzerV0_1(
            ReportMethodStarlinkDetectorV0_1(execution), config
        )

    def analyze_full_dwell(
        self, recording: RecordingView, request: StarlinkFullDwellRequestV0_1
    ) -> StarlinkFullDwellResponseBundleV0_1:
        if recording.manifest.recording_id != request.recording_id:
            raise ValueError("recording and full-dwell request identities differ")
        if request.search_grid != starlink_search_grid_v0_1(self._config):
            raise ValueError("full-dwell request selects another search grid")
        digest = request.recording_object_ref.identity_digest()
        provenance = Provenance(
            self._execution.producer_name,
            self._execution.producer_version,
            self._execution.git_commit,
            self._execution.environment_digest,
            request.search_grid.config_ref.digest,
            (digest,),
            (request.source_suite_ref.digest,),
            self._execution.started_utc_ns,
            self._execution.completed_utc_ns,
            self._execution.host_class,
        )
        segments = {item.segment_id: item for item in recording.manifest.segments}
        grouped: dict[SegmentId, list[StarlinkFullDwellStreamSelectionV0_1]] = (
            defaultdict(list)
        )
        for selection in request.stream_selections:
            grouped[selection.segment_id].append(selection)
        streams: list[StarlinkFullDwellStreamResponseV0_1] = []
        for segment_id in sorted(grouped, key=str):
            segment = segments.get(segment_id)
            if segment is None:
                raise ValueError("selected full-dwell segment is unavailable")
            receiver_count = len(segment.requested.receiver_chain_ids)
            tags = dict(segment.requested.tags)
            try:
                channel = int(tags["channel"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("full-dwell stream lacks a channel tag") from error
            for selection in grouped[segment_id]:
                if (
                    selection.segment_sample_count != segment.sample_count
                    or selection.sample_rate_hz != segment.actual_sample_rate_hz
                ):
                    raise ValueError("full-dwell selection differs from manifest")
                try:
                    receiver_index = segment.requested.receiver_chain_ids.index(
                        selection.receiver_chain_id
                    )
                except ValueError as error:
                    raise ValueError(
                        "selected full-dwell receiver is unavailable"
                    ) from error
                streams.append(
                    self._stream(
                        recording,
                        request,
                        selection,
                        segment,
                        channel,
                        receiver_count,
                        receiver_index,
                        digest,
                    )
                )
        streams.sort(
            key=lambda item: (
                str(item.segment_id),
                str(item.receiver_chain_id),
                item.edge.value,
            )
        )
        token = canonical_digest(
            {
                "request_digest": request.digest,
                "streams": tuple(canonical_digest(item) for item in streams),
            }
        ).value
        return StarlinkFullDwellResponseBundleV0_1(
            SchemaRef(StarlinkFullDwellResponseBundleV0_1.SCHEMA_ID, V0_1),
            f"slfd_{token[:32]}",
            request.recording_id,
            digest,
            request.source_suite_ref,
            request.source_suite_request_digest,
            request.digest,
            request.search_grid,
            request.plan,
            tuple(streams),
            provenance,
            (
                "candidate-evidence-not-calibrated-detection",
                "finite-surrogate-rank-not-p-value",
                "dwell-and-search-look-elsewhere-not-calibrated",
                "overlapping-windows-statistically-dependent",
                "fine-refinement-selected-by-pattern-blind-power",
                "prescreen-window-union-covers-full-dwell",
                "exact-detector-windows-are-selected-not-full-coverage",
            ),
            None,
        )

    def _stream(
        self,
        recording: RecordingView,
        request: StarlinkFullDwellRequestV0_1,
        selection: StarlinkFullDwellStreamSelectionV0_1,
        segment: SegmentManifest,
        channel: int,
        receiver_count: int,
        receiver_index: int,
        recording_digest: Digest,
    ) -> StarlinkFullDwellStreamResponseV0_1:
        plan = request.plan
        prescreen_starts = covering_window_starts_v0_1(
            selection.segment_sample_count,
            plan.coarse_window_sample_count,
            plan.coarse_stride_samples,
        )
        if len(prescreen_starts) > plan.maximum_prescreen_window_count:
            raise ValueError(
                "exhaustive full-dwell prescreen exceeds its declared bound"
            )
        if plan.maximum_fine_window_count > MAXIMUM_EXACT_WINDOWS_PER_STREAM:
            raise ValueError("exact full-dwell plan exceeds its declared bound")
        candidate_power = []
        for start in prescreen_starts:
            samples = _receiver_samples(
                recording,
                selection.segment_id,
                start,
                start + plan.coarse_window_sample_count,
                receiver_count,
                receiver_index,
            )
            candidate_power.append((mean_complex_power_v0_1(samples), start))
        fine = tuple(
            sorted(
                start
                for _, start in sorted(
                    candidate_power, key=lambda item: (-item[0], item[1])
                )[: plan.maximum_fine_window_count]
            )
        )
        power_by_start = {start: score for score, start in candidate_power}
        prescreen_windows = tuple(
            StarlinkFullDwellPrescreenWindowV0_1(
                index,
                start,
                start + plan.coarse_window_sample_count,
                UtcNs(
                    int(segment.start_utc_ns)
                    + round(start * 1_000_000_000 / selection.sample_rate_hz)
                ),
                UtcNs(
                    int(segment.start_utc_ns)
                    + round(
                        (start + plan.coarse_window_sample_count)
                        * 1_000_000_000
                        / selection.sample_rate_hz
                    )
                ),
                power_by_start[start],
                start in fine,
            )
            for index, start in enumerate(prescreen_starts)
        )
        points: list[StarlinkFullDwellPointV0_1] = []
        for index, start in enumerate(fine):
            window_samples = plan.fine_window_sample_count
            samples = _receiver_samples(
                recording,
                selection.segment_id,
                start,
                start + window_samples,
                receiver_count,
                receiver_index,
            )
            prescreen = power_by_start[start]
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
                surrogate_count=plan.surrogate_count,
            )
            exact = {item.method: item for item in paired.exact.methods}
            controls = tuple(
                {item.method: item for item in surrogate.methods}
                for surrogate in paired.surrogates
            )
            utc_start = UtcNs(
                int(segment.start_utc_ns)
                + round(start * 1_000_000_000 / selection.sample_rate_hz)
            )
            utc_stop = UtcNs(
                int(segment.start_utc_ns)
                + round(
                    (start + window_samples) * 1_000_000_000 / selection.sample_rate_hz
                )
            )
            for method in REPORT_METHOD_ORDER:
                qin = _winner(exact[method], start)
                surrogates = tuple(
                    StarlinkFullDwellSurrogateV0_1(
                        surrogate_index,
                        paired.surrogates[surrogate_index].pattern.template_ref.digest,
                        _winner(control[method], start),
                    )
                    for surrogate_index, control in enumerate(controls)
                )
                scores = tuple(item.winner.score for item in surrogates)
                points.append(
                    StarlinkFullDwellPointV0_1(
                        request.recording_id,
                        recording.manifest.radio_id,
                        selection.segment_id,
                        selection.receiver_chain_id,
                        selection.edge,
                        method,
                        index,
                        StarlinkWindowTier.EXACT_REFINEMENT,
                        start,
                        start + window_samples,
                        utc_start,
                        utc_stop,
                        prescreen,
                        qin,
                        surrogates,
                        1 + sum(score >= qin.score for score in scores),
                        qin.score - max(scores),
                        f"{selection.segment_id}-{selection.receiver_chain_id}-adaptive-exact",
                    )
                )
        prescreen_covered = _union_sample_count(
            tuple(
                (start, start + plan.coarse_window_sample_count)
                for start in prescreen_starts
            )
        )
        exact_covered = _union_sample_count(
            tuple((start, start + plan.fine_window_sample_count) for start in fine)
        )
        overlap = max(
            0.0,
            (plan.coarse_window_sample_count - plan.coarse_stride_samples)
            / plan.coarse_window_sample_count,
        )
        return StarlinkFullDwellStreamResponseV0_1(
            recording.manifest.radio_id,
            selection.segment_id,
            selection.receiver_chain_id,
            channel,
            selection.edge,
            selection.sample_rate_hz,
            selection.segment_sample_count,
            prescreen_windows,
            fine,
            tuple(points),
            prescreen_covered,
            prescreen_covered / selection.segment_sample_count,
            exact_covered,
            exact_covered / selection.segment_sample_count,
            overlap,
            True,
        )


def _winner(
    method: StarlinkPatternMethodEvidenceV0_1, window_start: int
) -> StarlinkFullDwellWinnerV0_1:
    return StarlinkFullDwellWinnerV0_1(
        method.score,
        method.winning_epoch_sample,
        window_start + method.winning_epoch_sample,
        method.winning_coarse_cfo_hz,
        method.winning_residual_cfo_hz,
        method.effective_search_cell_count,
        method.search_mode,
    )


def _union_sample_count(intervals: tuple[tuple[int, int], ...]) -> int:
    if not intervals:
        return 0
    total = 0
    start, stop = intervals[0]
    for next_start, next_stop in intervals[1:]:
        if next_start <= stop:
            stop = max(stop, next_stop)
        else:
            total += stop - start
            start, stop = next_start, next_stop
    return total + stop - start
