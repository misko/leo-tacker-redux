"""Versioned suite composition for additive v0.3 acquisition/QAM evidence."""

from __future__ import annotations

from dataclasses import dataclass

from leo_flow.analysis.recording.quality import decode_ci16
from leo_flow.analysis.recording.starlink_acquired_constellation import (
    StarlinkAcquiredPilotConstellationAnalyzerV0_3,
)
from leo_flow.analysis.recording.starlink_acquisition import StarlinkAcquisitionV0_3
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.core import ArtifactRef, ReceiverChainId
from leo_flow.contracts.starlink_acquired_constellation import (
    StarlinkAcquiredPilotConstellationEvidenceV0_3,
)
from leo_flow.contracts.starlink_acquisition import StarlinkAcquisitionBundleV0_3
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
