"""Combined legacy waterfall job preparation with additive v0.2 Doppler work."""

from __future__ import annotations

from dataclasses import dataclass

from leo_flow.analysis.recording.waterfall_doppler_pipeline import (
    PreparedWaterfallDopplerV0_1,
    WaterfallDopplerPipelineV0_1,
)
from leo_flow.contracts.waterfall import WaterfallAnalyzerV0_1
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.storage.ports import RecordingObjectReader

from .waterfall_analysis import (
    PreparedWaterfallAnalysisV0_1,
    WaterfallAnalysisJobError,
    decode_waterfall_analysis_payload,
)


@dataclass(frozen=True)
class PreparedCombinedWaterfallAnalysisV0_1(PreparedWaterfallAnalysisV0_1):
    enhanced: PreparedWaterfallDopplerV0_1


class CombinedWaterfallAnalysisJobPreparerV0_1:
    """Open one exact recording and prepare legacy plus enhanced products."""

    def __init__(
        self,
        reader: RecordingObjectReader,
        legacy_analyzer: WaterfallAnalyzerV0_1,
        enhanced_pipeline: WaterfallDopplerPipelineV0_1,
    ) -> None:
        self._reader = reader
        self._legacy_analyzer = legacy_analyzer
        self._enhanced_pipeline = enhanced_pipeline

    def prepare(self, lease: JobLease) -> PreparedCombinedWaterfallAnalysisV0_1:
        if lease.job_type is not JobType.WATERFALL_ANALYSIS:
            raise WaterfallAnalysisJobError("worker accepts waterfall jobs only")
        request = decode_waterfall_analysis_payload(lease.payload)
        with self._reader.open(request.recording_object_ref) as recording:
            legacy = self._legacy_analyzer.analyze_waterfall(recording, request)
            enhanced = self._enhanced_pipeline.analyze(recording, request)
        return PreparedCombinedWaterfallAnalysisV0_1(request, legacy, enhanced)
