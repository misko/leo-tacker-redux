"""Bounded stratified temporal Qin-versus-surrogate analysis."""

from __future__ import annotations

from collections import defaultdict

from leo_flow.contracts.core import (
    Digest,
    Provenance,
    SchemaRef,
    SegmentId,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.starlink_detector_suite import (
    REPORT_METHOD_ORDER,
    StarlinkDetectorMethod,
)
from leo_flow.contracts.starlink_surrogate_null import (
    StarlinkPatternMethodEvidenceV0_1,
)
from leo_flow.contracts.starlink_temporal_pilot import (
    V0_1,
    StarlinkTemporalDwellMethodSummaryV0_1,
    StarlinkTemporalMethodPointV0_1,
    StarlinkTemporalPilotRecordingBundleV0_1,
    StarlinkTemporalPilotRequestV0_1,
    StarlinkTemporalStreamEvidenceV0_1,
    StarlinkTemporalStreamSelectionV0_1,
    StarlinkTemporalSurrogateWinnerV0_1,
    StarlinkTemporalWinnerV0_1,
)
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


def temporal_probe_starts_v0_1(
    sample_count: int,
    window_sample_count: int,
    nominal_stride_samples: int,
    maximum_probe_count: int,
) -> tuple[int, ...]:
    """Sample the complete temporal extent and always include the tail window."""
    if sample_count < window_sample_count:
        return ()
    last = sample_count - window_sample_count
    regular = list(range(0, last + 1, nominal_stride_samples))
    if not regular or regular[-1] != last:
        regular.append(last)
    if len(regular) <= maximum_probe_count:
        return tuple(regular)
    if maximum_probe_count == 1:
        return (0,)
    # Deterministic endpoint-preserving thinning. Rounded linear indices avoid a
    # first-window prefix bias while retaining both exact dwell endpoints.
    selected = {
        regular[round(index * (len(regular) - 1) / (maximum_probe_count - 1))]
        for index in range(maximum_probe_count)
    }
    return tuple(sorted(selected))


class ExactStarlinkTemporalPilotRecordingAnalyzerV0_1:
    """Search every declared temporal probe through one common detector port."""

    def __init__(
        self,
        config: StarlinkDetectorSuiteConfigV0_2,
        execution: AnalysisExecutionContext,
    ) -> None:
        self._config = config
        self._execution = execution
        self._analyzer = StarlinkPairedSurrogateAnalyzerV0_1(
            ReportMethodStarlinkDetectorV0_1(execution), config
        )

    def analyze_temporal_pilot(
        self,
        recording: RecordingView,
        request: StarlinkTemporalPilotRequestV0_1,
    ) -> StarlinkTemporalPilotRecordingBundleV0_1:
        if recording.manifest.recording_id != request.recording_id:
            raise ValueError("recording and temporal request identities differ")
        if request.search_grid != starlink_search_grid_v0_1(self._config):
            raise ValueError("temporal request selects another search grid")
        recording_digest = request.recording_object_ref.identity_digest()
        provenance = Provenance(
            self._execution.producer_name,
            self._execution.producer_version,
            self._execution.git_commit,
            self._execution.environment_digest,
            request.search_grid.config_ref.digest,
            (recording_digest,),
            (request.source_suite_ref.digest,),
            self._execution.started_utc_ns,
            self._execution.completed_utc_ns,
            self._execution.host_class,
        )
        if request.ineligible_reason is not None:
            return _bundle(request, recording_digest, (), provenance)

        segments = {item.segment_id: item for item in recording.manifest.segments}
        grouped: dict[SegmentId, list[StarlinkTemporalStreamSelectionV0_1]] = (
            defaultdict(list)
        )
        for selection in request.stream_selections:
            grouped[selection.segment_id].append(selection)

        completed: list[StarlinkTemporalStreamEvidenceV0_1] = []
        for segment_id in sorted(grouped, key=str):
            selections = grouped[segment_id]
            segment = segments.get(segment_id)
            if segment is None:
                raise ValueError("selected temporal segment is unavailable")
            if (
                segment.sample_count != selections[0].segment_sample_count
                or segment.actual_sample_rate_hz != selections[0].sample_rate_hz
            ):
                raise ValueError("temporal selection differs from recording manifest")
            starts = temporal_probe_starts_v0_1(
                segment.sample_count,
                request.plan.window_sample_count,
                request.plan.nominal_stride_samples,
                request.plan.maximum_probe_count,
            )
            per_stream: dict[object, list[StarlinkTemporalMethodPointV0_1]] = {
                selection.receiver_chain_id: [] for selection in selections
            }
            receiver_count = len(segment.requested.receiver_chain_ids)
            for probe_index, start in enumerate(starts):
                stop = start + request.plan.window_sample_count
                raw = recording.read_iq_bytes(segment_id, start, stop)
                values, count = decode_ci16(raw, receiver_count)
                if count != request.plan.window_sample_count:
                    raise ValueError("temporal reader returned another interval")
                for selection in selections:
                    try:
                        receiver_index = segment.requested.receiver_chain_ids.index(
                            selection.receiver_chain_id
                        )
                    except ValueError as error:
                        raise ValueError(
                            "selected temporal receiver is unavailable"
                        ) from error
                    stride = receiver_count * 2
                    offset = receiver_index * 2
                    samples = tuple(
                        complex(values[position], values[position + 1])
                        for position in range(offset, count * stride, stride)
                    )
                    signal = radio_signal_v0_1(
                        samples,
                        recording_id=request.recording_id,
                        recording_identity_digest=recording_digest,
                        segment_id=selection.segment_id,
                        receiver_chain_id=selection.receiver_chain_id,
                        edge=selection.edge,
                        sample_rate_hz=selection.sample_rate_hz,
                    )
                    paired = self._analyzer.analyze(
                        signal, surrogate_count=request.plan.surrogate_count
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
                        + round(stop * 1_000_000_000 / selection.sample_rate_hz)
                    )
                    for method in REPORT_METHOD_ORDER:
                        qin = _winner(exact[method])
                        surrogate_winners = tuple(
                            StarlinkTemporalSurrogateWinnerV0_1(
                                index,
                                paired.surrogates[index].pattern.template_ref.digest,
                                _winner(control[method]),
                            )
                            for index, control in enumerate(controls)
                        )
                        scores = tuple(item.winner.score for item in surrogate_winners)
                        per_stream[selection.receiver_chain_id].append(
                            StarlinkTemporalMethodPointV0_1(
                                probe_index,
                                start,
                                stop,
                                (start + stop) / 2,
                                utc_start,
                                utc_stop,
                                method,
                                qin,
                                surrogate_winners,
                                1 + sum(score >= qin.score for score in scores),
                                qin.score - max(scores),
                            )
                        )
            tags = dict(segment.requested.tags)
            try:
                channel = int(tags["channel"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("temporal stream lacks a channel tag") from error
            for selection in selections:
                points = tuple(per_stream[selection.receiver_chain_id])
                summaries = tuple(
                    _dwell_summary(method, points) for method in REPORT_METHOD_ORDER
                )
                analyzed = _union_sample_count(
                    tuple(
                        (start, start + request.plan.window_sample_count)
                        for start in starts
                    )
                )
                completed.append(
                    StarlinkTemporalStreamEvidenceV0_1(
                        recording.manifest.radio_id,
                        selection.segment_id,
                        selection.receiver_chain_id,
                        channel,
                        selection.edge,
                        selection.sample_rate_hz,
                        selection.segment_sample_count,
                        starts,
                        points,
                        summaries,
                        analyzed,
                        analyzed / selection.segment_sample_count,
                    )
                )
        completed.sort(
            key=lambda item: (str(item.segment_id), str(item.receiver_chain_id))
        )
        return _bundle(request, recording_digest, tuple(completed), provenance)


def _winner(method: StarlinkPatternMethodEvidenceV0_1) -> StarlinkTemporalWinnerV0_1:
    # Kept structural so detector method evidence remains behind its public port.
    return StarlinkTemporalWinnerV0_1(
        method.score,
        method.winning_epoch_sample,
        method.winning_coarse_cfo_hz,
        method.winning_residual_cfo_hz,
        method.effective_search_cell_count,
        method.search_mode,
    )


def _dwell_summary(
    method: StarlinkDetectorMethod,
    points: tuple[StarlinkTemporalMethodPointV0_1, ...],
) -> StarlinkTemporalDwellMethodSummaryV0_1:
    selected = tuple(item for item in points if item.method is method)
    qin_maximum = max(item.qin.score for item in selected)
    surrogate_maxima = tuple(
        max(item.surrogates[index].winner.score for item in selected)
        for index in range(len(selected[0].surrogates))
    )
    return StarlinkTemporalDwellMethodSummaryV0_1(
        method,
        qin_maximum,
        surrogate_maxima,
        1 + sum(value >= qin_maximum for value in surrogate_maxima),
        qin_maximum - max(surrogate_maxima),
        sum(item.qin_minus_max_surrogate > 0 for item in selected),
        len(selected),
    )


def _union_sample_count(intervals: tuple[tuple[int, int], ...]) -> int:
    total = 0
    start, stop = intervals[0]
    for next_start, next_stop in intervals[1:]:
        if next_start <= stop:
            stop = max(stop, next_stop)
        else:
            total += stop - start
            start, stop = next_start, next_stop
    return total + stop - start


def _bundle(
    request: StarlinkTemporalPilotRequestV0_1,
    recording_digest: Digest,
    streams: tuple[StarlinkTemporalStreamEvidenceV0_1, ...],
    provenance: Provenance,
) -> StarlinkTemporalPilotRecordingBundleV0_1:
    warnings = (
        (
            "candidate-evidence-not-calibrated-detection",
            "finite-surrogate-rank-not-p-value",
            "dwell-maxima-include-time-look-elsewhere",
            "overlapping-windows-statistically-dependent",
        )
        if streams
        else ("clipped-pilot-band",)
    )
    token = canonical_digest(
        {
            "request_digest": request.digest,
            "streams": tuple(
                canonical_digest((item.probe_starts, item.points, item.dwell_summaries))
                for item in streams
            ),
        }
    ).value
    return StarlinkTemporalPilotRecordingBundleV0_1(
        SchemaRef(StarlinkTemporalPilotRecordingBundleV0_1.SCHEMA_ID, V0_1),
        f"sltime_{token[:32]}",
        request.recording_id,
        recording_digest,
        request.source_suite_ref,
        request.source_suite_request_digest,
        request.digest,
        request.search_grid,
        request.plan,
        streams,
        provenance,
        warnings,
        None,
    )
