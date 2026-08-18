"""Bounded server-side composition for interactive recording evidence."""

from __future__ import annotations

import math

from leo_flow.contracts.core import SchemaRef
from leo_flow.contracts.dashboard_advanced_doppler import (
    PublishedAdvancedDopplerPathQueryPortV0_1,
    PublishedAdvancedDopplerPathV0_1,
    RecordingEvidenceAdvancedDopplerSeriesV0_1,
    RecordingEvidenceAdvancedDopplerTotalV0_1,
    RecordingEvidenceAdvancedDopplerViewV0_1,
    RecordingEvidenceAdvancedDopplerWindowV0_1,
)
from leo_flow.contracts.dashboard_doppler import (
    DopplerCandidateViewV0_1,
    DopplerVisualizationState,
    DopplerWaterfallLayer,
    RecordingDopplerVisualizationQueryPortV0_1,
)
from leo_flow.contracts.dashboard_recording import (
    RecordingCaptureDetailQueryPortV0_1,
    RecordingSegmentViewV0_1,
)
from leo_flow.contracts.dashboard_recording_evidence import (
    RecordingEvidenceContextQueryPortV0_1,
    RecordingEvidenceDopplerQueryV0_1,
    RecordingEvidenceDopplerSeriesV0_1,
    RecordingEvidenceDopplerTotalV0_1,
    RecordingEvidenceDopplerViewV0_1,
    RecordingEvidenceDopplerWindowV0_1,
    RecordingEvidenceReceiverV0_1,
    RecordingEvidenceRecordingV0_1,
)


class RecordingEvidenceDopplerQueryServiceV0_1:
    """Derive disclosed local slopes only from immutable published track points."""

    def __init__(
        self,
        contexts: RecordingEvidenceContextQueryPortV0_1,
        recording_details: RecordingCaptureDetailQueryPortV0_1,
        doppler: RecordingDopplerVisualizationQueryPortV0_1,
    ) -> None:
        self._contexts = contexts
        self._recording_details = recording_details
        self._doppler = doppler

    def recording_evidence_doppler(
        self, query: RecordingEvidenceDopplerQueryV0_1
    ) -> RecordingEvidenceDopplerViewV0_1:
        context = self._contexts.recording_evidence_context(query.recording_id)
        assignments = {
            (item.recording_id, item.receiver_chain_id): item
            for item in context.receivers
            if (not query.radio_ids or item.radio_id in query.radio_ids)
            and (not query.lnb_ids or item.lnb_id in query.lnb_ids)
            and (
                not query.receiver_chain_ids
                or item.receiver_chain_id in query.receiver_chain_ids
            )
        }
        candidates: list[
            tuple[
                RecordingEvidenceRecordingV0_1,
                RecordingEvidenceReceiverV0_1,
                RecordingSegmentViewV0_1,
                DopplerCandidateViewV0_1,
                str,
            ]
        ] = []
        observed_states: set[DopplerVisualizationState] = set()
        for recording in context.recordings:
            if query.radio_ids and recording.radio_id not in query.radio_ids:
                continue
            visualization = self._doppler.recording_doppler_visualization(
                recording.recording_id, DopplerWaterfallLayer.RESIDUAL
            )
            observed_states.add(visualization.state)
            if visualization.state is not DopplerVisualizationState.COMPLETE:
                continue
            detail = self._recording_details.recording_capture_detail(
                recording.recording_id
            )
            segments = {item.segment_id: item for item in detail.segments}
            provenance = {
                (item.segment_id, item.receiver_chain_id): item.basic.artifact_id
                for item in visualization.doppler_provenance
            }
            for candidate in visualization.candidates:
                assignment = assignments.get(
                    (recording.recording_id, candidate.receiver_chain_id)
                )
                segment = segments.get(candidate.segment_id)
                artifact_id = provenance.get(
                    (candidate.segment_id, candidate.receiver_chain_id)
                )
                if assignment is None or segment is None or artifact_id is None:
                    continue
                candidates.append(
                    (recording, assignment, segment, candidate, artifact_id)
                )

        original_count = sum(
            max(0, len(candidate.points) - 1) for _, _, _, candidate, _ in candidates
        )

        remaining = query.maximum_windows
        series: list[RecordingEvidenceDopplerSeriesV0_1] = []
        for recording, assignment, segment, candidate, artifact_id in candidates:
            windows: list[RecordingEvidenceDopplerWindowV0_1] = []
            for first, second in zip(
                candidate.points, candidate.points[1:], strict=False
            ):
                if remaining <= 0:
                    break
                delta_s = (
                    int(second.midpoint_utc_ns) - int(first.midpoint_utc_ns)
                ) / 1e9
                if delta_s <= 0:
                    continue
                start_sample = round(
                    (int(first.midpoint_utc_ns) - int(segment.started_utc_ns))
                    * segment.sample_rate_hz
                    / 1e9
                )
                stop_sample = round(
                    (int(second.midpoint_utc_ns) - int(segment.started_utc_ns))
                    * segment.sample_rate_hz
                    / 1e9
                )
                start_sample = max(0, min(segment.sample_count - 1, start_sample))
                stop_sample = max(
                    start_sample + 1, min(segment.sample_count, stop_sample)
                )
                windows.append(
                    RecordingEvidenceDopplerWindowV0_1(
                        len(windows),
                        start_sample,
                        stop_sample,
                        first.midpoint_utc_ns,
                        second.midpoint_utc_ns,
                        (second.frequency_hz - first.frequency_hz) / delta_s,
                        (first.frequency_hz + second.frequency_hz) / 2,
                        2,
                        "adjacent-published-track-points-linear-slope",
                    )
                )
                remaining -= 1
            if not windows:
                continue
            series.append(
                RecordingEvidenceDopplerSeriesV0_1(
                    recording.recording_id,
                    recording.radio_id,
                    assignment.lnb_id,
                    candidate.receiver_chain_id,
                    candidate.segment_id,
                    candidate.rank,
                    candidate.selected_model.value,
                    artifact_id,
                    RecordingEvidenceDopplerTotalV0_1(
                        candidate.drift_rate_hz_s,
                        candidate.drift_acceleration_hz_s2,
                        candidate.reference_utc_ns,
                        candidate.reference_frequency_hz,
                        candidate.inlier_count,
                        candidate.residual_rms_hz,
                        "published-blind-doppler-candidate-fit",
                    ),
                    tuple(windows),
                )
            )
        state = "complete" if series else _incomplete_state(observed_states)
        shown = sum(len(item.windows) for item in series)
        return RecordingEvidenceDopplerViewV0_1(
            SchemaRef(RecordingEvidenceDopplerViewV0_1.SCHEMA_ID),
            query.recording_id,
            state,
            True,
            None,
            tuple(series),
            original_count,
            shown < original_count,
            (
                "candidate-only-no-calibrated-detection",
                "window-slopes-derived-from-adjacent-published-track-points",
            ),
        )


class RecordingEvidenceAdvancedDopplerQueryServiceV0_1:
    """Join authoritative hardware to exact advanced-path-only Doppler rows."""

    def __init__(
        self,
        contexts: RecordingEvidenceContextQueryPortV0_1,
        doppler: RecordingDopplerVisualizationQueryPortV0_1,
        advanced_paths: PublishedAdvancedDopplerPathQueryPortV0_1,
    ) -> None:
        self._contexts = contexts
        self._doppler = doppler
        self._advanced_paths = advanced_paths

    def recording_evidence_advanced_doppler(
        self, query: RecordingEvidenceDopplerQueryV0_1
    ) -> RecordingEvidenceAdvancedDopplerViewV0_1:
        context = self._contexts.recording_evidence_context(query.recording_id)
        assignments = {
            (item.recording_id, item.receiver_chain_id): item
            for item in context.receivers
            if (not query.radio_ids or item.radio_id in query.radio_ids)
            and (not query.lnb_ids or item.lnb_id in query.lnb_ids)
            and (
                not query.receiver_chain_ids
                or item.receiver_chain_id in query.receiver_chain_ids
            )
        }
        eligible: list[
            tuple[
                RecordingEvidenceRecordingV0_1,
                RecordingEvidenceReceiverV0_1,
                PublishedAdvancedDopplerPathV0_1,
            ]
        ] = []
        observed_states: set[DopplerVisualizationState] = set()
        for recording in context.recordings:
            if query.radio_ids and recording.radio_id not in query.radio_ids:
                continue
            visualization = self._doppler.recording_doppler_visualization(
                recording.recording_id, DopplerWaterfallLayer.RESIDUAL
            )
            observed_states.add(visualization.state)
            if visualization.state is not DopplerVisualizationState.COMPLETE:
                continue
            for path in self._advanced_paths.recording_advanced_doppler_paths(
                recording.recording_id
            ):
                assignment = assignments.get(
                    (recording.recording_id, path.receiver_chain_id)
                )
                if assignment is not None:
                    eligible.append((recording, assignment, path))

        original_count = sum(len(path.points) - 1 for _, _, path in eligible)
        remaining = query.maximum_windows
        series: list[RecordingEvidenceAdvancedDopplerSeriesV0_1] = []
        for recording, assignment, path in eligible:
            windows: list[RecordingEvidenceAdvancedDopplerWindowV0_1] = []
            for first, second in zip(path.points, path.points[1:], strict=False):
                if remaining <= 0:
                    break
                delta_s = (
                    int(second.midpoint_utc_ns) - int(first.midpoint_utc_ns)
                ) / 1e9
                if delta_s <= 0:
                    continue
                windows.append(
                    RecordingEvidenceAdvancedDopplerWindowV0_1(
                        len(windows),
                        first.start_sample,
                        second.stop_sample,
                        first.interval_start_utc_ns,
                        second.interval_stop_utc_ns,
                        first.midpoint_utc_ns,
                        second.midpoint_utc_ns,
                        (second.frequency_hz - first.frequency_hz) / delta_s,
                        (first.frequency_hz + second.frequency_hz) / 2,
                        2,
                        "adjacent-published-advanced-path-points-linear-slope",
                    )
                )
                remaining -= 1
            if not windows:
                continue
            reference = path.points[len(path.points) // 2]
            residual_rms_hz = math.sqrt(
                sum(
                    (
                        point.frequency_hz
                        - (
                            reference.frequency_hz
                            + path.published_drift_rate_hz_s
                            * (
                                int(point.midpoint_utc_ns)
                                - int(reference.midpoint_utc_ns)
                            )
                            / 1e9
                        )
                    )
                    ** 2
                    for point in path.points
                )
                / len(path.points)
            )
            series.append(
                RecordingEvidenceAdvancedDopplerSeriesV0_1(
                    recording.recording_id,
                    recording.radio_id,
                    assignment.lnb_id,
                    path.receiver_chain_id,
                    path.segment_id,
                    path.path_digest,
                    path.provenance_artifact_id,
                    path.association_state,
                    RecordingEvidenceAdvancedDopplerTotalV0_1(
                        path.published_drift_rate_hz_s,
                        reference.midpoint_utc_ns,
                        reference.frequency_hz,
                        len(path.points),
                        residual_rms_hz,
                        "published-advanced-slope-bank-path-rate",
                    ),
                    tuple(windows),
                )
            )
        state = "complete" if series else _incomplete_state(observed_states)
        shown = sum(len(item.windows) for item in series)
        return RecordingEvidenceAdvancedDopplerViewV0_1(
            SchemaRef(RecordingEvidenceAdvancedDopplerViewV0_1.SCHEMA_ID),
            query.recording_id,
            state,
            True,
            None,
            tuple(series),
            original_count,
            shown < original_count,
            (
                "advanced-path-only-not-a-calibrated-detection",
                "window-slopes-derived-from-adjacent-published-advanced-path-points",
            ),
        )


def _incomplete_state(states: set[DopplerVisualizationState]) -> str:
    if DopplerVisualizationState.PENDING in states:
        return "pending"
    if DopplerVisualizationState.FAILED in states:
        return "error"
    return "missing"
