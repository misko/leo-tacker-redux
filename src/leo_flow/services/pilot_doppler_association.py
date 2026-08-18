"""Candidate-only comparison of acquired-pilot CFO and blind Doppler paths."""

from __future__ import annotations

import bisect
import math
import statistics

from leo_flow.analysis.qam_goodness import qam_goodness_v0_2
from leo_flow.contracts.core import SchemaRef, UtcNs
from leo_flow.contracts.dashboard_advanced_doppler import (
    RecordingEvidenceAdvancedDopplerQueryPortV0_1,
    RecordingEvidenceAdvancedDopplerSeriesV0_1,
)
from leo_flow.contracts.dashboard_pilot_doppler import (
    MAXIMUM_PILOT_DOPPLER_SERIES,
    PILOT_DOPPLER_FREQUENCY_GATE_HZ,
    PilotDopplerAssociationQueryV0_1,
    PilotDopplerAssociationSeriesV0_1,
    PilotDopplerDistancePointV0_1,
    PilotDopplerPathComparisonV0_1,
    PilotQamFrequencyFitV0_1,
    PilotQamFrequencyWindowV0_1,
    RecordingPilotDopplerAssociationViewV0_1,
)
from leo_flow.contracts.dashboard_recording import RecordingCaptureDetailQueryPortV0_1
from leo_flow.contracts.dashboard_recording_evidence import (
    RecordingEvidenceDopplerQueryV0_1,
)
from leo_flow.contracts.starlink_acquired_constellation_pipeline import (
    MAX_ACQUIRED_QAM_QUERY_STREAMS,
    StarlinkAcquiredConstellationQueryV0_3,
    StarlinkAcquiredConstellationViewMode,
)
from leo_flow.contracts.starlink_adaptive_qam import (
    RecordingStarlinkAdaptiveQamQueryPortV0_4,
)


class RecordingPilotDopplerAssociationServiceV0_1:
    def __init__(
        self,
        recordings: RecordingCaptureDetailQueryPortV0_1,
        qam: RecordingStarlinkAdaptiveQamQueryPortV0_4,
        doppler: RecordingEvidenceAdvancedDopplerQueryPortV0_1,
    ) -> None:
        self._recordings, self._qam, self._doppler = recordings, qam, doppler

    def recording_pilot_doppler_association(
        self, query: PilotDopplerAssociationQueryV0_1
    ) -> RecordingPilotDopplerAssociationViewV0_1:
        detail = self._recordings.recording_capture_detail(query.recording_id)
        centers = {
            segment.segment_id: segment.center_frequency_hz
            for segment in detail.segments
        }
        qam = self._qam.recording_starlink_adaptive_qam(
            StarlinkAcquiredConstellationQueryV0_3(
                query.recording_id,
                StarlinkAcquiredConstellationViewMode.WINDOWS,
                query.radio_ids,
                query.lnb_ids,
                (),
                query.receiver_chain_ids,
                query.edges,
                MAX_ACQUIRED_QAM_QUERY_STREAMS,
                query.maximum_windows_per_stream,
                1,
            )
        )
        advanced = self._doppler.recording_evidence_advanced_doppler(
            RecordingEvidenceDopplerQueryV0_1(
                query.recording_id,
                query.radio_ids,
                query.lnb_ids,
                query.receiver_chain_ids,
                4096,
            )
        )
        paths: dict[
            tuple[str, str, str], list[RecordingEvidenceAdvancedDopplerSeriesV0_1]
        ] = {}
        for path in advanced.series:
            if path.recording_id != query.recording_id:
                continue
            paths.setdefault(
                (
                    str(path.segment_id),
                    str(path.receiver_chain_id),
                    path.lnb_id,
                ),
                [],
            ).append(path)

        series: list[PilotDopplerAssociationSeriesV0_1] = []
        for stream in qam.streams:
            center = centers.get(stream.segment_id)
            if center is None:
                continue
            windows = tuple(
                PilotQamFrequencyWindowV0_1(
                    item.qam.window_index,
                    item.qam.start_sample,
                    item.qam.stop_sample,
                    item.qam.interval_start_utc_ns,
                    item.qam.interval_stop_utc_ns,
                    item.qam.winning_cfo_hz,
                    center + item.qam.winning_cfo_hz,
                    qam_goodness_v0_2(item.qam.hard_symbol_accuracy, item.qam.rms_evm),
                    item.qam.hard_symbol_accuracy,
                    item.qam.rms_evm,
                )
                for item in stream.windows
            )
            if len(windows) < 2:
                continue
            fit = _pilot_fit(windows)
            comparisons = tuple(
                _compare_path(windows, fit, path)
                for path in paths.get(
                    (
                        str(stream.segment_id),
                        str(stream.receiver_chain_id),
                        stream.lnb_id,
                    ),
                    (),
                )
            )
            series.append(
                PilotDopplerAssociationSeriesV0_1(
                    query.recording_id,
                    stream.radio_id,
                    stream.lnb_id,
                    stream.receiver_chain_id,
                    stream.segment_id,
                    stream.edge,
                    center,
                    windows,
                    fit,
                    comparisons,
                )
            )
            if len(series) >= MAXIMUM_PILOT_DOPPLER_SERIES:
                break
        state = (
            "complete"
            if series
            else ("pending" if advanced.state == "pending" else "missing")
        )
        return RecordingPilotDopplerAssociationViewV0_1(
            SchemaRef(RecordingPilotDopplerAssociationViewV0_1.SCHEMA_ID),
            query.recording_id,
            state,
            PILOT_DOPPLER_FREQUENCY_GATE_HZ,
            tuple(series),
            True,
            None,
            (
                "blind-path-frequency-must-overlap-acquired-pilot-frequency",
                "candidate-only-no-calibrated-detection",
                "pilot-fit-selects-qam-goodness-at-least-0.5-diagnostic-only",
            ),
        )


def _pilot_fit(
    windows: tuple[PilotQamFrequencyWindowV0_1, ...],
) -> PilotQamFrequencyFitV0_1:
    ranked = sorted(
        windows,
        key=lambda item: (-item.qam_goodness, item.window_index),
    )
    separated = [item for item in ranked if item.qam_goodness >= 0.5]
    chosen = separated[:8] if len(separated) >= 2 else ranked[:2]
    selected = tuple(
        sorted(
            chosen,
            key=lambda item: int(item.interval_start_utc_ns),
        )
    )
    reference_ns = sorted(
        (int(item.interval_start_utc_ns) + int(item.interval_stop_utc_ns)) // 2
        for item in selected
    )[len(selected) // 2]
    times = tuple(
        (
            (int(item.interval_start_utc_ns) + int(item.interval_stop_utc_ns)) / 2
            - reference_ns
        )
        / 1e9
        for item in selected
    )
    frequencies = tuple(item.absolute_frequency_hz for item in selected)
    mean_time = statistics.fmean(times)
    mean_frequency = statistics.fmean(frequencies)
    denominator = math.fsum((value - mean_time) ** 2 for value in times)
    drift = (
        math.fsum(
            (time - mean_time) * (frequency - mean_frequency)
            for time, frequency in zip(times, frequencies, strict=True)
        )
        / denominator
        if denominator
        else 0.0
    )
    reference_frequency = mean_frequency + drift * (0.0 - mean_time)
    residual = math.sqrt(
        math.fsum(
            (frequency - (reference_frequency + drift * time)) ** 2
            for time, frequency in zip(times, frequencies, strict=True)
        )
        / len(selected)
    )
    return PilotQamFrequencyFitV0_1(
        UtcNs(reference_ns),
        reference_frequency,
        drift,
        residual,
        len(selected),
        "qam-goodness-at-least-0.5-up-to-eight-diagnostic-with-top-two-fallback",
    )


def _compare_path(
    windows: tuple[PilotQamFrequencyWindowV0_1, ...],
    fit: PilotQamFrequencyFitV0_1,
    path: RecordingEvidenceAdvancedDopplerSeriesV0_1,
) -> PilotDopplerPathComparisonV0_1:
    path_times = tuple(
        (int(item.point_start_utc_ns) + int(item.point_stop_utc_ns)) // 2
        for item in path.windows
    )
    path_frequencies = tuple(item.midpoint_frequency_hz for item in path.windows)
    points = []
    for window in windows:
        midpoint = (
            int(window.interval_start_utc_ns) + int(window.interval_stop_utc_ns)
        ) // 2
        frequency = _interpolate(midpoint, path_times, path_frequencies)
        if frequency is None:
            continue
        points.append(
            PilotDopplerDistancePointV0_1(
                window.window_index,
                UtcNs(midpoint),
                window.absolute_frequency_hz,
                frequency,
                abs(window.absolute_frequency_hz - frequency),
            )
        )
    distances = tuple(item.absolute_distance_hz for item in points)
    minimum = min(distances) if distances else 0.0
    median = statistics.median(distances) if distances else 0.0
    if len(points) < 2:
        state = "insufficient-time-overlap"
    elif median <= PILOT_DOPPLER_FREQUENCY_GATE_HZ:
        state = "frequency-compatible-candidate"
    else:
        state = "frequency-mismatch"
    difference = path.total.drift_rate_hz_s - fit.drift_rate_hz_s
    return PilotDopplerPathComparisonV0_1(
        path.path_digest,
        state,
        path.total.drift_rate_hz_s,
        fit.drift_rate_hz_s,
        difference,
        minimum,
        median,
        PILOT_DOPPLER_FREQUENCY_GATE_HZ,
        tuple(points),
    )


def _interpolate(
    target: int, times: tuple[int, ...], frequencies: tuple[float, ...]
) -> float | None:
    if not times or target < times[0] or target > times[-1]:
        return None
    index = bisect.bisect_left(times, target)
    if index == 0:
        return frequencies[0]
    if index == len(times):
        return frequencies[-1]
    before_time, after_time = times[index - 1], times[index]
    before_frequency, after_frequency = frequencies[index - 1], frequencies[index]
    if after_time == before_time:
        return before_frequency
    fraction = (target - before_time) / (after_time - before_time)
    return before_frequency + fraction * (after_frequency - before_frequency)
