"""Versioned suite composition for additive v0.3 acquisition/QAM evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass

from leo_flow.analysis.recording.quality import decode_ci16
from leo_flow.analysis.recording.starlink_acquired_constellation import (
    StarlinkAcquiredPilotConstellationAnalyzerV0_3,
)
from leo_flow.analysis.recording.starlink_acquisition import StarlinkAcquisitionV0_3
from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteV0_2,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    ReceiverChainId,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.starlink_acquired_constellation import (
    StarlinkAcquiredPilotConstellationEvidenceV0_3,
)
from leo_flow.contracts.starlink_acquired_constellation_pipeline import (
    MAX_ACQUIRED_QAM_WINDOWS_PER_STREAM,
    StarlinkAcquiredConstellationOverallV0_3,
    StarlinkAcquiredConstellationRecordingBundleV0_3,
    StarlinkAcquiredConstellationRequestV0_3,
    StarlinkAcquiredConstellationStreamV0_3,
    StarlinkAcquiredConstellationWindowV0_3,
)
from leo_flow.contracts.starlink_acquisition import V0_3, StarlinkAcquisitionBundleV0_3
from leo_flow.contracts.starlink_suite_pipeline import StarlinkSuiteRecordingState
from leo_flow.jobs.contracts import JobLease
from leo_flow.storage.ports import RecordingObjectReader

from .starlink_suite_surrogate_analysis import (
    CombinedStarlinkSuiteAnalysisJobPreparerV0_2,
    PreparedCombinedStarlinkSuiteAnalysisV0_2,
)


@dataclass(frozen=True)
class StarlinkAcquisitionCompositionProfileV0_3:
    suite_config_ref: ArtifactRef
    receiver_chain_id: ReceiverChainId
    acquisition_analyzer: StarlinkAcquisitionV0_3
    constellation_analyzer: StarlinkAcquiredPilotConstellationAnalyzerV0_3


@dataclass(frozen=True)
class PreparedCombinedStarlinkSuiteAnalysisV0_3(
    PreparedCombinedStarlinkSuiteAnalysisV0_2
):
    acquisitions_v0_3: tuple[StarlinkAcquisitionBundleV0_3, ...] = ()
    acquired_constellations_v0_3: tuple[
        StarlinkAcquiredPilotConstellationEvidenceV0_3, ...
    ] = ()


class CombinedStarlinkSuiteAnalysisJobPreparerV0_3:
    """Preserve v0.2/v0.1 outputs and append exact-profile v0.3 products."""

    def __init__(
        self,
        reader: RecordingObjectReader,
        legacy: CombinedStarlinkSuiteAnalysisJobPreparerV0_2,
        profiles: tuple[StarlinkAcquisitionCompositionProfileV0_3, ...],
    ) -> None:
        if not profiles:
            raise ValueError("v0.3 composition requires acquisition profiles")
        keys = tuple(
            (item.suite_config_ref, item.receiver_chain_id) for item in profiles
        )
        if len(keys) != len(set(keys)):
            raise ValueError("v0.3 acquisition composition profiles are ambiguous")
        self._reader = reader
        self._legacy = legacy
        self._profiles = profiles

    def prepare(self, lease: JobLease) -> PreparedCombinedStarlinkSuiteAnalysisV0_3:
        prepared = self._legacy.prepare(lease)
        if prepared.bundle.state is StarlinkSuiteRecordingState.NOT_EVALUATED:
            return PreparedCombinedStarlinkSuiteAnalysisV0_3(
                prepared.request,
                prepared.bundle,
                prepared.surrogate_null,
                prepared.pilot_constellation,
                prepared.temporal_pilot,
            )
        profiles = {
            item.receiver_chain_id: item
            for item in self._profiles
            if item.suite_config_ref == prepared.request.config_ref
        }
        required = {item.receiver_chain_id for item in prepared.bundle.suites}
        if set(profiles) != required:
            raise ValueError("eligible suite has no exact receiver acquisition profile")
        acquisitions = []
        constellations = []
        with self._reader.open(prepared.request.recording_object_ref) as recording:
            segments = {item.segment_id: item for item in recording.manifest.segments}
            for suite in prepared.bundle.suites:
                segment = segments[suite.segment_id]
                receiver_index = segment.requested.receiver_chain_ids.index(
                    suite.receiver_chain_id
                )
                raw = recording.read_iq_bytes(
                    suite.segment_id, 0, suite.probe_sample_count
                )
                values, count = decode_ci16(
                    raw, len(segment.requested.receiver_chain_ids)
                )
                if count != suite.probe_sample_count:
                    raise ValueError(
                        "v0.3 acquisition reader returned another interval"
                    )
                stride = len(segment.requested.receiver_chain_ids) * 2
                offset = receiver_index * 2
                samples = tuple(
                    complex(values[position], values[position + 1])
                    for position in range(offset, count * stride, stride)
                )
                templates = qin_edge_pilot_template_pair_v0_1(
                    suite.sample_rate_hz, suite.edge
                )
                profile = profiles[suite.receiver_chain_id]
                acquisition = profile.acquisition_analyzer.analyze_receiver(
                    samples,
                    recording_id=suite.recording_id,
                    recording_identity_digest=suite.recording_identity_digest,
                    segment_id=suite.segment_id,
                    receiver_chain_id=suite.receiver_chain_id,
                    templates=templates,
                )
                acquisitions.append(acquisition)
                constellations.append(
                    profile.constellation_analyzer.analyze(samples, suite, acquisition)
                )
        return PreparedCombinedStarlinkSuiteAnalysisV0_3(
            prepared.request,
            prepared.bundle,
            prepared.surrogate_null,
            prepared.pilot_constellation,
            prepared.temporal_pilot,
            tuple(acquisitions),
            tuple(constellations),
        )


@dataclass(frozen=True)
class StarlinkAcquiredDwellCompositionProfileV0_3:
    suite_config_ref: ArtifactRef
    receiver_chain_id: ReceiverChainId
    suite_analyzer: StarlinkDetectorSuiteV0_2
    acquisition_analyzer: StarlinkAcquisitionV0_3
    constellation_analyzer: StarlinkAcquiredPilotConstellationAnalyzerV0_3


@dataclass(frozen=True)
class PreparedStarlinkAcquiredConstellationV0_3:
    request: StarlinkAcquiredConstellationRequestV0_3
    bundle: StarlinkAcquiredConstellationRecordingBundleV0_3


@dataclass(frozen=True)
class PreparedCombinedStarlinkSuiteDwellAnalysisV0_3(
    PreparedCombinedStarlinkSuiteAnalysisV0_2
):
    acquired_constellation_v0_3: PreparedStarlinkAcquiredConstellationV0_3 | None = None


class CombinedStarlinkSuiteDwellAnalysisJobPreparerV0_3:
    """Add bounded, evenly spaced acquired-QAM windows across every dwell."""

    def __init__(
        self,
        reader: RecordingObjectReader,
        legacy: CombinedStarlinkSuiteAnalysisJobPreparerV0_2,
        profiles: tuple[StarlinkAcquiredDwellCompositionProfileV0_3, ...],
        *,
        window_sample_count: int = 50_000,
        maximum_windows_per_stream: int = MAX_ACQUIRED_QAM_WINDOWS_PER_STREAM,
    ) -> None:
        if (
            window_sample_count <= 0
            or not 1
            <= maximum_windows_per_stream
            <= MAX_ACQUIRED_QAM_WINDOWS_PER_STREAM
        ):
            raise ValueError("invalid acquired-QAM dwell plan")
        keys = tuple(
            (item.suite_config_ref, item.receiver_chain_id) for item in profiles
        )
        if not profiles or len(keys) != len(set(keys)):
            raise ValueError("acquired-QAM dwell profiles are empty or ambiguous")
        self._reader, self._legacy, self._profiles = reader, legacy, profiles
        self._window_sample_count = window_sample_count
        self._maximum_windows_per_stream = maximum_windows_per_stream

    def prepare(
        self, lease: JobLease
    ) -> PreparedCombinedStarlinkSuiteDwellAnalysisV0_3:
        prepared = self._legacy.prepare(lease)
        if prepared.bundle.state is StarlinkSuiteRecordingState.NOT_EVALUATED:
            return PreparedCombinedStarlinkSuiteDwellAnalysisV0_3(
                prepared.request,
                prepared.bundle,
                prepared.surrogate_null,
                prepared.pilot_constellation,
                prepared.temporal_pilot,
                None,
            )
        profile_map = {
            (item.suite_config_ref, item.receiver_chain_id): item
            for item in self._profiles
        }
        with self._reader.open(prepared.request.recording_object_ref) as recording:
            segments = {item.segment_id: item for item in recording.manifest.segments}
            radio_id = recording.manifest.radio_id
            streams = []
            for source_suite in prepared.bundle.suites:
                try:
                    profile = profile_map[
                        (prepared.request.config_ref, source_suite.receiver_chain_id)
                    ]
                    segment = segments[source_suite.segment_id]
                    receiver_index = segment.requested.receiver_chain_ids.index(
                        source_suite.receiver_chain_id
                    )
                except (KeyError, ValueError) as error:
                    raise ValueError(
                        "eligible suite has no exact acquired-QAM dwell profile"
                    ) from error
                if segment.sample_count < self._window_sample_count:
                    raise ValueError("acquired-QAM window exceeds segment")
                starts = _bounded_window_starts(
                    segment.sample_count,
                    self._window_sample_count,
                    self._maximum_windows_per_stream,
                )
                templates = qin_edge_pilot_template_pair_v0_1(
                    segment.actual_sample_rate_hz, source_suite.edge
                )
                windows = []
                for index, start in enumerate(starts):
                    raw = recording.read_iq_bytes(
                        source_suite.segment_id, start, self._window_sample_count
                    )
                    values, count = decode_ci16(
                        raw, len(segment.requested.receiver_chain_ids)
                    )
                    if count != self._window_sample_count:
                        raise ValueError(
                            "acquired-QAM dwell reader returned another interval"
                        )
                    stride = len(segment.requested.receiver_chain_ids) * 2
                    offset = receiver_index * 2
                    samples = tuple(
                        complex(values[position], values[position + 1])
                        for position in range(offset, count * stride, stride)
                    )
                    suite = profile.suite_analyzer.analyze_receiver(
                        samples,
                        recording_id=prepared.request.recording_id,
                        recording_identity_digest=prepared.request.recording_object_ref.identity_digest(),
                        segment_id=source_suite.segment_id,
                        receiver_chain_id=source_suite.receiver_chain_id,
                        templates=templates,
                    )
                    acquisition = profile.acquisition_analyzer.analyze_receiver(
                        samples,
                        recording_id=prepared.request.recording_id,
                        recording_identity_digest=prepared.request.recording_object_ref.identity_digest(),
                        segment_id=source_suite.segment_id,
                        receiver_chain_id=source_suite.receiver_chain_id,
                        templates=templates,
                    )
                    evidence = profile.constellation_analyzer.analyze(
                        samples, suite, acquisition, time_window_count=len(starts)
                    )
                    start_utc = int(segment.start_utc_ns) + round(
                        start / segment.actual_sample_rate_hz * 1_000_000_000
                    )
                    stop = start + count
                    stop_utc = int(segment.start_utc_ns) + round(
                        stop / segment.actual_sample_rate_hz * 1_000_000_000
                    )
                    windows.append(
                        StarlinkAcquiredConstellationWindowV0_3(
                            index,
                            start,
                            stop,
                            UtcNs(start_utc),
                            UtcNs(stop_utc),
                            acquisition,
                            evidence,
                        )
                    )
                streams.append(
                    StarlinkAcquiredConstellationStreamV0_3(
                        radio_id,
                        source_suite.segment_id,
                        source_suite.receiver_chain_id,
                        source_suite.edge,
                        segment.actual_sample_rate_hz,
                        segment.sample_count,
                        tuple(windows),
                        _overall(tuple(windows)),
                    )
                )
        streams.sort(
            key=lambda item: (
                str(item.radio_id),
                str(item.segment_id),
                str(item.receiver_chain_id),
                item.edge.value,
            )
        )
        stream_keys = tuple(
            (item.radio_id, item.segment_id, item.receiver_chain_id, item.edge)
            for item in streams
        )
        request = StarlinkAcquiredConstellationRequestV0_3(
            SchemaRef(StarlinkAcquiredConstellationRequestV0_3.SCHEMA_ID, V0_3),
            prepared.request.recording_id,
            prepared.request.recording_object_ref,
            ArtifactRef(
                prepared.bundle.analysis_id,
                prepared.bundle.digest,
                prepared.bundle.schema,
            ),
            canonical_digest(prepared.request),
            stream_keys,
            self._maximum_windows_per_stream,
            SchemaRef(StarlinkAcquiredConstellationRecordingBundleV0_3.SCHEMA_ID, V0_3),
        )
        token = canonical_digest(
            {
                "request_digest": str(request.digest),
                "stream_digests": tuple(
                    str(canonical_digest(item)) for item in streams
                ),
            }
        ).value
        bundle = StarlinkAcquiredConstellationRecordingBundleV0_3(
            SchemaRef(StarlinkAcquiredConstellationRecordingBundleV0_3.SCHEMA_ID, V0_3),
            f"slqam3rec_{token[:32]}",
            request.recording_id,
            request.recording_object_ref.identity_digest(),
            request.source_suite_ref,
            request.source_suite_request_digest,
            request.digest,
            tuple(streams),
            (
                "candidate-evidence-not-calibrated-detection",
                "whole-revised-search-calibration-required",
                "published-edge-pilot-not-user-payload",
                "bounded-window-sampling-across-dwell",
            ),
            None,
        )
        return PreparedCombinedStarlinkSuiteDwellAnalysisV0_3(
            prepared.request,
            prepared.bundle,
            prepared.surrogate_null,
            prepared.pilot_constellation,
            prepared.temporal_pilot,
            PreparedStarlinkAcquiredConstellationV0_3(request, bundle),
        )


def _bounded_window_starts(
    segment_sample_count: int, window_sample_count: int, maximum_windows: int
) -> tuple[int, ...]:
    natural = math.ceil(segment_sample_count / window_sample_count)
    count = min(natural, maximum_windows)
    if count == 1:
        return (0,)
    extent = segment_sample_count - window_sample_count
    return tuple(round(index * extent / (count - 1)) for index in range(count))


def _overall(
    windows: tuple[StarlinkAcquiredConstellationWindowV0_3, ...],
) -> StarlinkAcquiredConstellationOverallV0_3:
    support = sum(item.evidence.complete_frame_count for item in windows)

    def weighted(values: tuple[float, ...]) -> float:
        return (
            sum(
                value * item.evidence.complete_frame_count
                for value, item in zip(values, windows, strict=True)
            )
            / support
        )

    selected = max(
        range(len(windows)),
        key=lambda index: (
            windows[index].evidence.verify_minus_control_margin,
            windows[index].evidence.held_out_verify_score,
            -index,
        ),
    )
    return StarlinkAcquiredConstellationOverallV0_3(
        len(windows),
        support,
        weighted(tuple(item.evidence.hard_symbol_accuracy for item in windows)),
        weighted(tuple(item.evidence.rms_evm for item in windows)),
        weighted(tuple(item.evidence.model_snr_db for item in windows)),
        max(item.evidence.held_out_verify_score for item in windows),
        max(item.evidence.verify_minus_control_margin for item in windows),
        selected,
    )
