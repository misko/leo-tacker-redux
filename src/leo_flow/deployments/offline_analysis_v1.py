"""Offline two-stage analysis composition with exact scientific plugins.

This module performs no I/O at import time and intentionally exports no global
``DeploymentPlugin``. A station deployment must inject the durable adapters and
the exact, versioned scientific implementations it has approved.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from leo_flow.analysis.dataset import DatasetSnapshotReader
from leo_flow.contracts.core import ArtifactRef
from leo_flow.contracts.features import FeatureSetBundle, RecordingAnalysisRequest
from leo_flow.contracts.model import (
    FeatureDatasetSnapshot,
    ModelAnalysisRequest,
    ModelSnapshotBundle,
)
from leo_flow.contracts.ports import (
    EphemerisReader,
    FeatureSetReader,
    HardwareMetadataReader,
    ModelFitter,
    RecordingAnalyzer,
)
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.jobs.ports import JobLeaseRepository, StaleLeaseError
from leo_flow.services.analysis import build_analysis_service
from leo_flow.services.config import AnalysisServiceConfig
from leo_flow.services.lifecycle import DiagnosticSink, ServiceLoop
from leo_flow.services.model_analysis import (
    ModelAnalysisCommitter,
    ModelAnalysisJobPreparer,
    ModelAnalysisJobProcessor,
    ModelFitterFactory,
)
from leo_flow.services.recording_analysis import (
    FencedRecordingAnalysisWorker,
    RecordingAnalysisCommitter,
    RecordingAnalysisJobPreparer,
)
from leo_flow.storage.ports import RecordingObjectReader, RecordingView


class OfflineAnalysisCompositionError(RuntimeError):
    """The selected offline job or scientific implementation is unavailable."""


@dataclass(frozen=True)
class AlgorithmKey:
    """Exact immutable algorithm/config identity used for plugin selection."""

    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef


class ExactRecordingAnalyzerRegistry:
    """Dispatch one recording to an explicitly registered exact implementation."""

    def __init__(self, analyzers: Mapping[AlgorithmKey, RecordingAnalyzer]) -> None:
        if not analyzers:
            raise ValueError("at least one recording analyzer is required")
        self._analyzers = MappingProxyType(dict(analyzers))

    def analyze(
        self, recording: RecordingView, request: RecordingAnalysisRequest
    ) -> FeatureSetBundle:
        try:
            analyzer = self._analyzers[
                AlgorithmKey(request.algorithm_ref, request.config_ref)
            ]
        except KeyError as error:
            raise OfflineAnalysisCompositionError(
                "exact recording algorithm and configuration are not registered"
            ) from error
        return analyzer.analyze(recording, request)


ModelFitterBuilder = Callable[[FeatureDatasetSnapshot], ModelFitter]


class ExactModelFitterRegistry:
    """Build a request-checking fitter over an immutable exact registry."""

    def __init__(self, fitters: Mapping[AlgorithmKey, ModelFitterBuilder]) -> None:
        if not fitters:
            raise ValueError("at least one model fitter is required")
        self._fitters = MappingProxyType(dict(fitters))

    def __call__(self, dataset: FeatureDatasetSnapshot) -> ModelFitter:
        return _RequestSelectedFitter(dataset, self._fitters)


@dataclass(frozen=True)
class _RequestSelectedFitter:
    dataset: FeatureDatasetSnapshot
    fitters: Mapping[AlgorithmKey, ModelFitterBuilder]

    def fit(
        self,
        request: ModelAnalysisRequest,
        features: FeatureSetReader,
        ephemerides: EphemerisReader,
        hardware: HardwareMetadataReader,
    ) -> ModelSnapshotBundle:
        key = AlgorithmKey(request.algorithm_ref, request.model_config_ref)
        try:
            fitter = self.fitters[key](self.dataset)
        except KeyError as error:
            raise OfflineAnalysisCompositionError(
                "exact model algorithm and configuration are not registered"
            ) from error
        return fitter.fit(request, features, ephemerides, hardware)


@dataclass(frozen=True)
class OfflineAnalysisComponents:
    """Infrastructure and scientific seams required by the offline process."""

    jobs: JobLeaseRepository
    recordings: RecordingObjectReader
    recording_analyzer: RecordingAnalyzer
    recording_committer: RecordingAnalysisCommitter
    datasets: DatasetSnapshotReader
    features: FeatureSetReader
    ephemerides: EphemerisReader
    hardware: HardwareMetadataReader
    model_fitter_factory: ModelFitterFactory
    model_committer: ModelAnalysisCommitter
    preflight: Callable[[], None] = lambda: None
    close: Callable[[float], None] = lambda timeout_s: None


class _LeaseExecutor(Protocol):
    def execute(self, lease: JobLease) -> object: ...


class FencedModelAnalysisExecutor:
    """Give preparation failures the same explicit fenced state as recording jobs."""

    def __init__(self, jobs: JobLeaseRepository, processor: _LeaseExecutor) -> None:
        self._jobs = jobs
        self._processor = processor

    def execute(self, lease: JobLease) -> object:
        if lease.job_type is not JobType.MODEL_ANALYSIS:
            raise OfflineAnalysisCompositionError(
                "model executor received a different job type"
            )
        try:
            return self._processor.execute(lease)
        except Exception as error:
            try:
                self._jobs.fail(
                    lease.job_id,
                    lease.lease_token,
                    lease.lease_generation,
                    f"{type(error).__name__}: model analysis failed",
                    None,
                )
            except StaleLeaseError:
                pass
            raise


class OfflineAnalysisCycle:
    """Route only independent-recording and cross-recording analysis jobs."""

    CLAIMED_TYPES = (JobType.RECORDING_ANALYSIS, JobType.MODEL_ANALYSIS)

    def __init__(
        self,
        jobs: JobLeaseRepository,
        *,
        recording: _LeaseExecutor,
        model: _LeaseExecutor,
        worker_id: str,
        lease_ttl_s: float,
        preflight: Callable[[], None] = lambda: None,
        close: Callable[[float], None] = lambda timeout_s: None,
    ) -> None:
        if not worker_id or lease_ttl_s <= 0:
            raise ValueError("worker identity and positive lease TTL are required")
        self._jobs = jobs
        self._executors = MappingProxyType(
            {
                JobType.RECORDING_ANALYSIS: recording,
                JobType.MODEL_ANALYSIS: model,
            }
        )
        self._worker_id = worker_id
        self._lease_ttl_s = lease_ttl_s
        self._preflight = preflight
        self._close = close

    def preflight(self) -> None:
        self._preflight()

    def process_one_job(self) -> bool:
        lease = self._jobs.claim(self.CLAIMED_TYPES, self._worker_id, self._lease_ttl_s)
        if lease is None:
            return False
        try:
            executor = self._executors[lease.job_type]
        except KeyError as error:
            raise OfflineAnalysisCompositionError(
                "claimed job kind is outside offline analysis"
            ) from error
        executor.execute(lease)
        return True

    def close(self, timeout_s: float) -> None:
        self._close(timeout_s)


def build_offline_analysis_cycle(
    config: AnalysisServiceConfig,
    components: OfflineAnalysisComponents,
    *,
    lease_ttl_s: float,
) -> OfflineAnalysisCycle:
    """Compose durable executors without importing capture or provider clients."""

    recording = FencedRecordingAnalysisWorker(
        components.jobs,
        RecordingAnalysisJobPreparer(
            components.recordings, components.recording_analyzer
        ),
        components.recording_committer,
        worker_id=config.runtime.instance_id,
        lease_ttl_s=lease_ttl_s,
    )
    model = FencedModelAnalysisExecutor(
        components.jobs,
        ModelAnalysisJobProcessor(
            ModelAnalysisJobPreparer(
                components.datasets,
                components.features,
                components.ephemerides,
                components.hardware,
                components.model_fitter_factory,
            ),
            components.model_committer,
        ),
    )
    return OfflineAnalysisCycle(
        components.jobs,
        recording=recording,
        model=model,
        worker_id=config.runtime.instance_id,
        lease_ttl_s=lease_ttl_s,
        preflight=components.preflight,
        close=components.close,
    )


def build_offline_analysis_service(
    config: AnalysisServiceConfig,
    components: OfflineAnalysisComponents,
    *,
    lease_ttl_s: float,
    diagnostics: DiagnosticSink | None = None,
) -> ServiceLoop:
    """Build the runnable lifecycle around the strict two-stage cycle."""

    return build_analysis_service(
        config,
        build_offline_analysis_cycle(config, components, lease_ttl_s=lease_ttl_s),
        diagnostics=diagnostics,
    )
