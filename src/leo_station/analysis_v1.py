"""Exact, checked scientific plugin for post-capture analysis on Gauss.

Importing this module performs no filesystem, database, network, radio, or clock
I/O.  The immutable values below are the development approval boundary: queued
recording work must name the exact algorithm, configuration, and environment
dependency exported here.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from leo_flow.analysis.model import (
    ModelExecutionContext,
    ReceiverQualityAggregateConfig,
    ReceiverQualityAggregateModel,
    receiver_quality_aggregate_algorithm_ref,
    receiver_quality_aggregate_config_ref,
)
from leo_flow.analysis.recording import (
    AnalysisConfigurationError,
    AnalysisExecutionContext,
    BoundedWaterfallAnalyzerV0_1,
    FullCoverageWaterfallAnalyzerV0_2,
    KnownCodePilotSearchConfigV0_1,
    KnownCodePilotSearchV0_1,
    QualityPsdAnalyzer,
    QualityPsdConfig,
    StarlinkDetectorSuiteConfigV0_2,
    StarlinkDetectorSuiteV0_2,
    WaterfallConfigV0_1,
    WaterfallConfigV0_2,
    known_code_pilot_algorithm_ref_v0_1,
    known_code_pilot_config_ref_v0_1,
    qin_edge_pilot_template_pair_v0_1,
    quality_psd_algorithm_ref,
    quality_psd_config_ref,
    starlink_detector_suite_algorithm_ref_v0_2,
    starlink_detector_suite_config_ref_v0_2,
    waterfall_algorithm_ref_v0_1,
    waterfall_algorithm_ref_v0_2,
    waterfall_config_ref_v0_1,
    waterfall_config_ref_v0_2,
)
from leo_flow.analysis.recording.starlink_pilot_constellation import (
    StarlinkPilotConstellationAnalyzerV0_1,
    StarlinkPilotConstellationConfigV0_1,
    starlink_pilot_constellation_algorithm_ref_v0_1,
    starlink_pilot_constellation_config_ref_v0_1,
)
from leo_flow.analysis.recording.starlink_recording import (
    ExactKnownCodeRecordingAnalyzerV0_1,
)
from leo_flow.analysis.recording.starlink_suite_recording import (
    ExactStarlinkDetectorSuiteRecordingAnalyzerV0_2,
)
from leo_flow.analysis.recording.starlink_surrogate_null import (
    starlink_search_grid_v0_1,
)
from leo_flow.analysis.recording.starlink_surrogate_null_recording import (
    ExactStarlinkSurrogateNullRecordingAnalyzerV0_1,
)
from leo_flow.analysis.recording.starlink_temporal_pilot_recording import (
    ExactStarlinkTemporalPilotRecordingAnalyzerV0_1,
)
from leo_flow.analysis.recording.waterfall_doppler_pipeline import (
    WaterfallDopplerPipelineV0_1,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.features import FeatureSetBundle, RecordingAnalysisRequest
from leo_flow.contracts.model import FeatureDatasetSnapshot
from leo_flow.contracts.starlink import (
    StarlinkEdge,
    StarlinkPilotAnalysisBundleV0_1,
)
from leo_flow.contracts.starlink_pipeline import StarlinkPilotAnalysisRequestV0_1
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteRecordingBundleV0_2,
    StarlinkDetectorSuiteRequestV0_2,
)
from leo_flow.contracts.waterfall import (
    WaterfallAnalysisRequestV0_1,
    WaterfallBundleV0_1,
)
from leo_flow.contracts.waterfall_v0_2 import WaterfallBundleV0_2
from leo_flow.deployments.offline_analysis_v1 import (
    AlgorithmKey,
    StationScientificFactories,
    build_station_plugin,
)
from leo_flow.services.bootstrap import AdapterSet, DeploymentPlugin, Process
from leo_flow.services.config import ServiceConfig
from leo_flow.services.lifecycle import DiagnosticSink, ServiceLoop
from leo_flow.services.starlink_surrogate_null_analysis import (
    StarlinkSurrogateNullAnalysisPreparerV0_1,
)
from leo_flow.services.starlink_temporal_pilot_analysis import (
    StarlinkTemporalPilotAnalysisPreparerV0_1,
)
from leo_flow.storage.ports import RecordingObjectReader, RecordingView

PLUGIN_ID: Final = "gauss-analysis-v1"
SOURCE_COMMIT: Final = "21c1aef9f7e7f057d0f6adfa985daafa7e47b6f3"
SOURCE_COMMIT_UTC_NS: Final = UtcNs(1_786_999_298_000_000_000)
APPROVED_PYTHON: Final = (3, 11, 16)
APPROVED_PYTHON_VERSION: Final = "3.11.16"
CAS_ROOT: Final = Path("/home/mouse9911/.local/share/leo-flow/objects")
MODE_LOCK_PATH: Final = Path("/home/mouse9911/.local/state/leo-flow/pipeline-mode.lock")


class GaussRuntimeApprovalError(RuntimeError):
    """The executing interpreter differs from the approved science runtime."""


RECORDING_CONFIG: Final = QualityPsdConfig(
    psd_window_samples=256,
    psd_stride_samples=1_000_000,
    clip_threshold_abs=2040,
    dc_warning_fraction=0.25,
    noise_floor_epsilon=1e-12,
)
WATERFALL_CONFIG: Final = WaterfallConfigV0_1(
    fft_window_samples=256,
    frequency_bins=128,
    maximum_time_bins_per_tile=128,
    maximum_tiles=64,
    maximum_total_cells=262_144,
    power_floor_counts_squared=1e-12,
)
WATERFALL_CONFIG_V0_2: Final = WaterfallConfigV0_2()
_STARLINK_CFO_HYPOTHESES_HZ: Final = tuple(
    float(value) for value in range(-100_000, 100_001, 20_000)
)
STARLINK_CONFIG_2M5: Final = KnownCodePilotSearchConfigV0_1(
    epoch_hypotheses_samples=tuple(range(0, 3_334, 64)),
    cfo_hypotheses_hz=_STARLINK_CFO_HYPOTHESES_HZ,
    maximum_search_cells=1_024,
    maximum_probe_samples=20_000,
)
STARLINK_CONFIG_5M: Final = KnownCodePilotSearchConfigV0_1(
    epoch_hypotheses_samples=tuple(range(0, 6_667, 128)),
    cfo_hypotheses_hz=_STARLINK_CFO_HYPOTHESES_HZ,
    maximum_search_cells=1_024,
    maximum_probe_samples=40_000,
)
MODEL_CONFIG: Final = ReceiverQualityAggregateConfig(
    minimum_feature_sets=2,
    score_variance_key="score_variance",
    covariance_floor=1e-12,
)


@dataclass(frozen=True)
class GaussStarlinkSuiteProfileV0_2:
    sample_rate_hz: float
    probe_sample_count: int
    config: StarlinkDetectorSuiteConfigV0_2
    config_ref: ArtifactRef
    eligible: bool


def _suite_config(sample_rate_hz: float) -> StarlinkDetectorSuiteConfigV0_2:
    stride = (
        32
        if sample_rate_hz == 1_250_000.0
        else (64 if sample_rate_hz == 2_500_000.0 else 128)
    )
    period = round(sample_rate_hz / 750.0)
    return StarlinkDetectorSuiteConfigV0_2(
        epoch_hypotheses_samples=tuple(range(0, period, stride)),
        coarse_cfo_hypotheses_hz=_STARLINK_CFO_HYPOTHESES_HZ,
    )


STARLINK_SUITE_PROFILES: Final = tuple(
    GaussStarlinkSuiteProfileV0_2(
        rate,
        probe,
        config,
        starlink_detector_suite_config_ref_v0_2(config),
        rate >= 1_875_000.0,
    )
    for rate, probe, config in (
        (1_250_000.0, 10_000, _suite_config(1_250_000.0)),
        (2_500_000.0, 20_000, _suite_config(2_500_000.0)),
        (5_000_000.0, 40_000, _suite_config(5_000_000.0)),
    )
)


def _starlink_template_approval(
    sample_rate_hz: float, edge: StarlinkEdge
) -> dict[str, object]:
    pair = qin_edge_pilot_template_pair_v0_1(sample_rate_hz, edge)
    return {
        "edge": edge.value,
        "exact": str(pair.exact_ref.digest),
        "conditioned_control": str(pair.conditioned_control_ref.digest),
    }


_STARLINK_SCIENCE_APPROVAL: Final = tuple(
    {
        "sample_rate_hz": sample_rate_hz,
        "config_digest": str(known_code_pilot_config_ref_v0_1(config).digest),
        "template_digests": tuple(
            _starlink_template_approval(sample_rate_hz, edge)
            for edge in (StarlinkEdge.LOWER, StarlinkEdge.UPPER)
        ),
    }
    for sample_rate_hz, config in (
        (2_500_000.0, STARLINK_CONFIG_2M5),
        (5_000_000.0, STARLINK_CONFIG_5M),
    )
)

DEPENDENCY_LOCK_REF: Final = ArtifactRef(
    "leo-tracker-redux-uv-lock-v1",
    Digest(
        DigestAlgorithm.SHA256,
        "2f34c93399474a48973214cc6c7b4ac2e37860a831bc66b5c93e6060610e5875",
    ),
    SchemaRef("org.leo-flow.python-dependency-lock"),
)
ENVIRONMENT_REF: Final = ArtifactRef(
    "gauss-python31116-analysis-environment-v1",
    canonical_digest(
        {
            "host": "gauss",
            "implementation": PLUGIN_ID,
            "python": APPROVED_PYTHON_VERSION,
            "dependency_lock_digest": str(DEPENDENCY_LOCK_REF.digest),
            "source_commit": SOURCE_COMMIT,
            "science": (
                str(quality_psd_algorithm_ref().digest),
                str(waterfall_algorithm_ref_v0_1().digest),
                str(waterfall_algorithm_ref_v0_2().digest),
                str(waterfall_config_ref_v0_2(WATERFALL_CONFIG_V0_2).digest),
                str(known_code_pilot_algorithm_ref_v0_1().digest),
                _STARLINK_SCIENCE_APPROVAL,
                str(starlink_detector_suite_algorithm_ref_v0_2().digest),
                tuple(
                    (
                        profile.sample_rate_hz,
                        str(profile.config_ref.digest),
                        profile.eligible,
                    )
                    for profile in STARLINK_SUITE_PROFILES
                ),
                str(receiver_quality_aggregate_algorithm_ref().digest),
            ),
        }
    ),
    SchemaRef("org.leo-flow.analysis-environment"),
)
RECORDING_ALGORITHM_REF: Final = quality_psd_algorithm_ref()
RECORDING_CONFIG_REF: Final = quality_psd_config_ref(RECORDING_CONFIG)
RECORDING_DEPENDENCY_REFS: Final = (ENVIRONMENT_REF, DEPENDENCY_LOCK_REF)
WATERFALL_ALGORITHM_REF: Final = waterfall_algorithm_ref_v0_1()
WATERFALL_CONFIG_REF: Final = waterfall_config_ref_v0_1(WATERFALL_CONFIG)
WATERFALL_DEPENDENCY_REFS: Final = (ENVIRONMENT_REF, DEPENDENCY_LOCK_REF)
WATERFALL_ALGORITHM_REF_V0_2: Final = waterfall_algorithm_ref_v0_2()
WATERFALL_CONFIG_REF_V0_2: Final = waterfall_config_ref_v0_2(WATERFALL_CONFIG_V0_2)
STARLINK_ALGORITHM_REF: Final = known_code_pilot_algorithm_ref_v0_1()
STARLINK_SUITE_ALGORITHM_REF: Final = starlink_detector_suite_algorithm_ref_v0_2()
STARLINK_PILOT_CONSTELLATION_CONFIG: Final = StarlinkPilotConstellationConfigV0_1()
MODEL_ALGORITHM_REF: Final = receiver_quality_aggregate_algorithm_ref()
MODEL_CONFIG_REF: Final = receiver_quality_aggregate_config_ref(MODEL_CONFIG)


@dataclass(frozen=True)
class GaussStarlinkSearchProfileV0_1:
    sample_rate_hz: float
    probe_sample_count: int
    config: KnownCodePilotSearchConfigV0_1
    config_ref: ArtifactRef


STARLINK_SEARCH_PROFILES: Final = (
    GaussStarlinkSearchProfileV0_1(
        2_500_000.0,
        20_000,
        STARLINK_CONFIG_2M5,
        known_code_pilot_config_ref_v0_1(STARLINK_CONFIG_2M5),
    ),
    GaussStarlinkSearchProfileV0_1(
        5_000_000.0,
        40_000,
        STARLINK_CONFIG_5M,
        known_code_pilot_config_ref_v0_1(STARLINK_CONFIG_5M),
    ),
)


def starlink_search_profile_v0_1(
    sample_rate_hz: float,
) -> GaussStarlinkSearchProfileV0_1:
    matches = tuple(
        profile
        for profile in STARLINK_SEARCH_PROFILES
        if profile.sample_rate_hz == sample_rate_hz
    )
    if len(matches) != 1:
        raise ValueError("recording sample rate has no approved Qin search profile")
    return matches[0]


def starlink_suite_profile_v0_2(sample_rate_hz: float) -> GaussStarlinkSuiteProfileV0_2:
    matches = tuple(
        profile
        for profile in STARLINK_SUITE_PROFILES
        if profile.sample_rate_hz == sample_rate_hz
    )
    if len(matches) != 1:
        raise ValueError("recording sample rate has no approved detector-suite profile")
    return matches[0]


def _recording_execution() -> AnalysisExecutionContext:
    return AnalysisExecutionContext(
        producer_name="leo-flow-gauss-quality-psd",
        producer_version="0.1.0",
        git_commit=SOURCE_COMMIT,
        environment_digest=ENVIRONMENT_REF.digest,
        started_utc_ns=SOURCE_COMMIT_UTC_NS,
        completed_utc_ns=SOURCE_COMMIT_UTC_NS,
        host_class="gauss-x86_64-python31116",
    )


def _model_execution() -> ModelExecutionContext:
    return ModelExecutionContext(
        producer_name="leo-flow-gauss-receiver-quality-aggregate",
        producer_version="0.1.0",
        git_commit=SOURCE_COMMIT,
        environment_digest=ENVIRONMENT_REF.digest,
        started_utc_ns=SOURCE_COMMIT_UTC_NS,
        completed_utc_ns=SOURCE_COMMIT_UTC_NS,
        host_class="gauss-x86_64-python31116",
    )


def _waterfall_execution() -> AnalysisExecutionContext:
    return AnalysisExecutionContext(
        producer_name="leo-flow-gauss-waterfall",
        producer_version="0.1.0",
        git_commit=SOURCE_COMMIT,
        environment_digest=ENVIRONMENT_REF.digest,
        started_utc_ns=SOURCE_COMMIT_UTC_NS,
        completed_utc_ns=SOURCE_COMMIT_UTC_NS,
        host_class="gauss-x86_64-python31116",
    )


def _waterfall_v0_2_execution() -> AnalysisExecutionContext:
    return AnalysisExecutionContext(
        producer_name="leo-flow-gauss-full-coverage-waterfall-doppler",
        producer_version="0.2.0",
        git_commit=SOURCE_COMMIT,
        environment_digest=ENVIRONMENT_REF.digest,
        started_utc_ns=SOURCE_COMMIT_UTC_NS,
        completed_utc_ns=SOURCE_COMMIT_UTC_NS,
        host_class="gauss-x86_64-python31116",
    )


def _starlink_execution() -> AnalysisExecutionContext:
    return AnalysisExecutionContext(
        producer_name="leo-flow-gauss-starlink-known-code-candidates",
        producer_version="0.1.0",
        git_commit=SOURCE_COMMIT,
        environment_digest=ENVIRONMENT_REF.digest,
        started_utc_ns=SOURCE_COMMIT_UTC_NS,
        completed_utc_ns=SOURCE_COMMIT_UTC_NS,
        host_class="gauss-x86_64-python31116",
    )


def _starlink_suite_execution() -> AnalysisExecutionContext:
    return AnalysisExecutionContext(
        producer_name="leo-flow-gauss-starlink-detector-suite",
        producer_version="0.2.0",
        git_commit=SOURCE_COMMIT,
        environment_digest=ENVIRONMENT_REF.digest,
        started_utc_ns=SOURCE_COMMIT_UTC_NS,
        completed_utc_ns=SOURCE_COMMIT_UTC_NS,
        host_class="gauss-x86_64-python31116",
    )


def _starlink_pilot_constellation_execution() -> AnalysisExecutionContext:
    return AnalysisExecutionContext(
        producer_name="leo-flow-gauss-starlink-published-pilot-constellation",
        producer_version="0.1.0",
        git_commit=SOURCE_COMMIT,
        environment_digest=ENVIRONMENT_REF.digest,
        started_utc_ns=SOURCE_COMMIT_UTC_NS,
        completed_utc_ns=SOURCE_COMMIT_UTC_NS,
        host_class="gauss-x86_64-python31116",
    )


@dataclass(frozen=True)
class _ExactDependencyAnalyzer:
    delegate: QualityPsdAnalyzer
    dependency_refs: tuple[ArtifactRef, ...]

    def analyze(
        self, recording: RecordingView, request: RecordingAnalysisRequest
    ) -> FeatureSetBundle:
        if request.dependency_refs != self.dependency_refs:
            raise ValueError(
                "recording request dependency refs differ from Gauss approval"
            )
        return self.delegate.analyze(recording, request)


@dataclass(frozen=True)
class ExactGaussWaterfallAnalyzerV0_1:
    delegate: BoundedWaterfallAnalyzerV0_1
    dependency_refs: tuple[ArtifactRef, ...]

    def analyze_waterfall(
        self, recording: RecordingView, request: WaterfallAnalysisRequestV0_1
    ) -> WaterfallBundleV0_1:
        if request.dependency_refs != self.dependency_refs:
            raise AnalysisConfigurationError(
                "waterfall dependency refs differ from Gauss approval"
            )
        return self.delegate.analyze_waterfall(recording, request)


WATERFALL_ANALYZER: Final = ExactGaussWaterfallAnalyzerV0_1(
    BoundedWaterfallAnalyzerV0_1(WATERFALL_CONFIG, _waterfall_execution()),
    WATERFALL_DEPENDENCY_REFS,
)
WATERFALL_DOPPLER_PIPELINE: Final = WaterfallDopplerPipelineV0_1(
    FullCoverageWaterfallAnalyzerV0_2(
        WATERFALL_CONFIG_V0_2, _waterfall_v0_2_execution()
    ),
    WATERFALL_CONFIG_V0_2,
)
_STARLINK_ANALYZERS: Final = tuple(
    (
        profile,
        ExactKnownCodeRecordingAnalyzerV0_1(
            KnownCodePilotSearchV0_1(profile.config, _starlink_execution()),
            algorithm_ref=STARLINK_ALGORITHM_REF,
            config_ref=profile.config_ref,
        ),
    )
    for profile in STARLINK_SEARCH_PROFILES
)


@dataclass(frozen=True)
class ExactGaussStarlinkAnalyzerV0_1:
    """Dispatch only an exact approved rate/config pair for selected streams."""

    def analyze_starlink(
        self,
        recording: RecordingView,
        request: StarlinkPilotAnalysisRequestV0_1,
    ) -> StarlinkPilotAnalysisBundleV0_1:
        segments = {
            segment.segment_id: segment for segment in recording.manifest.segments
        }
        try:
            rates = {
                segments[selection.segment_id].actual_sample_rate_hz
                for selection in request.stream_selections
            }
        except KeyError as error:
            raise ValueError("selected Starlink segment is unavailable") from error
        if len(rates) != 1:
            raise ValueError("Starlink request mixes recorded sample rates")
        profile = starlink_search_profile_v0_1(next(iter(rates)))
        if request.config_ref != profile.config_ref:
            raise ValueError("Starlink request config differs from its recorded rate")
        delegates = tuple(
            delegate
            for candidate, delegate in _STARLINK_ANALYZERS
            if candidate == profile
        )
        if len(delegates) != 1:
            raise RuntimeError("approved Starlink analyzer registry is ambiguous")
        return delegates[0].analyze_starlink(recording, request)


STARLINK_ANALYZER: Final = ExactGaussStarlinkAnalyzerV0_1()


@dataclass(frozen=True)
class ExactGaussStarlinkSuiteAnalyzerV0_2:
    def analyze_starlink_suite(
        self, recording: RecordingView, request: StarlinkDetectorSuiteRequestV0_2
    ) -> StarlinkDetectorSuiteRecordingBundleV0_2:
        rates = {
            segment.actual_sample_rate_hz for segment in recording.manifest.segments
        }
        if len(rates) != 1:
            raise ValueError("detector-suite recording mixes rates")
        profile = starlink_suite_profile_v0_2(next(iter(rates)))
        if (
            request.algorithm_ref != STARLINK_SUITE_ALGORITHM_REF
            or request.config_ref != profile.config_ref
        ):
            raise ValueError("detector-suite request differs from rate approval")
        if (request.ineligible_reason is None) != profile.eligible:
            raise ValueError("detector-suite eligibility differs from rate profile")
        analyzer = ExactStarlinkDetectorSuiteRecordingAnalyzerV0_2(
            StarlinkDetectorSuiteV0_2(profile.config, _starlink_suite_execution())
        )
        return analyzer.analyze_starlink_suite(recording, request)


STARLINK_SUITE_ANALYZER: Final = ExactGaussStarlinkSuiteAnalyzerV0_2()
STARLINK_PILOT_CONSTELLATION_ANALYZER: Final = StarlinkPilotConstellationAnalyzerV0_1(
    STARLINK_PILOT_CONSTELLATION_CONFIG,
    _starlink_pilot_constellation_execution(),
)


def starlink_surrogate_null_preparers_v0_1(
    reader: RecordingObjectReader,
) -> tuple[tuple[ArtifactRef, StarlinkSurrogateNullAnalysisPreparerV0_1], ...]:
    """Bind every exact suite profile to its paired-surrogate implementation."""

    return tuple(
        (
            profile.config_ref,
            StarlinkSurrogateNullAnalysisPreparerV0_1(
                reader,
                ExactStarlinkSurrogateNullRecordingAnalyzerV0_1(
                    profile.config, _starlink_suite_execution()
                ),
                starlink_search_grid_v0_1(profile.config),
            ),
        )
        for profile in STARLINK_SUITE_PROFILES
    )


def starlink_temporal_pilot_preparers_v0_1() -> tuple[
    tuple[ArtifactRef, StarlinkTemporalPilotAnalysisPreparerV0_1], ...
]:
    """Bind each approved suite profile to stratified temporal evidence."""

    return tuple(
        (
            profile.config_ref,
            StarlinkTemporalPilotAnalysisPreparerV0_1(
                ExactStarlinkTemporalPilotRecordingAnalyzerV0_1(
                    profile.config, _starlink_suite_execution()
                ),
                starlink_search_grid_v0_1(profile.config),
            ),
        )
        for profile in STARLINK_SUITE_PROFILES
    )


def _model_fitter(dataset: FeatureDatasetSnapshot) -> ReceiverQualityAggregateModel:
    return ReceiverQualityAggregateModel(dataset, MODEL_CONFIG, _model_execution())


SCIENTIFIC: Final = StationScientificFactories(
    recording_analyzers={
        AlgorithmKey(RECORDING_ALGORITHM_REF, RECORDING_CONFIG_REF): (
            _ExactDependencyAnalyzer(
                QualityPsdAnalyzer(RECORDING_CONFIG, _recording_execution()),
                RECORDING_DEPENDENCY_REFS,
            )
        )
    },
    model_fitters={AlgorithmKey(MODEL_ALGORITHM_REF, MODEL_CONFIG_REF): _model_fitter},
)


def require_approved_runtime() -> None:
    """Refuse scientific execution under a numerically different interpreter."""

    if tuple(sys.version_info[:3]) != APPROVED_PYTHON:
        raise GaussRuntimeApprovalError("Gauss science requires exact Python 3.11.16")


_BASE_PLUGIN = build_station_plugin(SCIENTIFIC, cas_root=CAS_ROOT)


def _build_analysis(
    config: ServiceConfig, adapters: AdapterSet, diagnostics: DiagnosticSink
) -> ServiceLoop:
    require_approved_runtime()
    return _BASE_PLUGIN.builders[Process.ANALYSIS](config, adapters, diagnostics)


# The only runnable capability is the durable offline analysis process.  The
# builder checks the interpreter before preflight can claim any work.
PLUGIN: Final = DeploymentPlugin(
    _BASE_PLUGIN.manifest,
    _BASE_PLUGIN.secret_providers,
    {Process.ANALYSIS: _build_analysis},
)


def _artifact_document(ref: ArtifactRef) -> dict[str, object]:
    return {
        "artifact_id": ref.artifact_id,
        "digest": {
            "algorithm": ref.digest.algorithm.value,
            "value": ref.digest.value,
        },
        "schema": (
            None
            if ref.schema is None
            else {
                "schema_id": ref.schema.schema_id,
                "version": str(ref.schema.version),
            }
        ),
    }


def _starlink_profile_document(
    profile: GaussStarlinkSearchProfileV0_1,
) -> dict[str, object]:
    templates = {
        edge.value: qin_edge_pilot_template_pair_v0_1(profile.sample_rate_hz, edge)
        for edge in (StarlinkEdge.LOWER, StarlinkEdge.UPPER)
    }
    return {
        "sample_rate_hz": profile.sample_rate_hz,
        "probe_sample_count": profile.probe_sample_count,
        "config_ref": _artifact_document(profile.config_ref),
        "epoch_hypothesis_count": len(profile.config.epoch_hypotheses_samples),
        "cfo_hypotheses_hz": list(profile.config.cfo_hypotheses_hz),
        "search_cell_count": profile.config.search_cell_count,
        "template_refs": {
            edge: {
                "exact": _artifact_document(pair.exact_ref),
                "conditioned_control": _artifact_document(pair.conditioned_control_ref),
            }
            for edge, pair in templates.items()
        },
    }


def science_manifest() -> dict[str, object]:
    """Return the exact JSON value an operator must present before any work."""

    return {
        "schema": "org.leo-flow.gauss-analysis-approval",
        "version": "0.1",
        "plugin_id": PLUGIN_ID,
        "source_commit": SOURCE_COMMIT,
        "cas_root": str(CAS_ROOT),
        "mode_lock_path": str(MODE_LOCK_PATH),
        "recording": {
            "algorithm_ref": _artifact_document(RECORDING_ALGORITHM_REF),
            "config_ref": _artifact_document(RECORDING_CONFIG_REF),
            "dependency_refs": [
                _artifact_document(ref) for ref in RECORDING_DEPENDENCY_REFS
            ],
            "requested_output_schema": {
                "schema_id": FeatureSetBundle.SCHEMA_ID,
                "version": "0.1",
            },
        },
        "waterfall": {
            "algorithm_ref": _artifact_document(WATERFALL_ALGORITHM_REF),
            "config_ref": _artifact_document(WATERFALL_CONFIG_REF),
            "dependency_refs": [
                _artifact_document(ref) for ref in WATERFALL_DEPENDENCY_REFS
            ],
            "requested_output_schema": {
                "schema_id": WaterfallBundleV0_1.SCHEMA_ID,
                "version": "0.1",
            },
        },
        "waterfall_v0_2_doppler": {
            "algorithm_ref": _artifact_document(WATERFALL_ALGORITHM_REF_V0_2),
            "config_ref": _artifact_document(WATERFALL_CONFIG_REF_V0_2),
            "dependency_refs": [
                _artifact_document(ref) for ref in WATERFALL_DEPENDENCY_REFS
            ],
            "requested_output_schema": {
                "schema_id": WaterfallBundleV0_2.SCHEMA_ID,
                "version": "0.2",
            },
            "doppler_semantics": "candidate-only-no-calibrated-detection",
        },
        "starlink_candidates": {
            "algorithm_ref": _artifact_document(STARLINK_ALGORITHM_REF),
            "profiles": [
                _starlink_profile_document(profile)
                for profile in STARLINK_SEARCH_PROFILES
            ],
            "template_source": "qin-appendix-a-frozen-v1",
            "decision_semantics": "candidates-only-calibration-required",
            "requested_output_schema": {
                "schema_id": "org.leo-flow.starlink-pilot-analysis-bundle",
                "version": "0.1",
            },
        },
        "starlink_detector_suite": {
            "algorithm_ref": _artifact_document(STARLINK_SUITE_ALGORITHM_REF),
            "profiles": [
                {
                    "sample_rate_hz": profile.sample_rate_hz,
                    "probe_sample_count": profile.probe_sample_count,
                    "config_ref": _artifact_document(profile.config_ref),
                    "eligible": profile.eligible,
                    "method_count_per_stream": 8,
                }
                for profile in STARLINK_SUITE_PROFILES
            ],
            "clipped_policy": "terminal-not-evaluated",
            "decision_semantics": "candidates-only-whole-search-calibration-required",
            "requested_output_schema": {
                "schema_id": "org.leo-flow.starlink-detector-suite-recording-bundle",
                "version": "0.2",
            },
        },
        "starlink_pilot_constellation": {
            "algorithm_ref": _artifact_document(
                starlink_pilot_constellation_algorithm_ref_v0_1()
            ),
            "config_ref": _artifact_document(
                starlink_pilot_constellation_config_ref_v0_1(
                    STARLINK_PILOT_CONSTELLATION_CONFIG
                )
            ),
            "source": "full-frame-acquire-winner-from-detector-suite-v0.2",
            "scope": "published-edge-pilot-not-user-payload",
            "decision_semantics": "candidate-evidence-not-calibrated-detection",
        },
        "starlink_temporal_pilot": {
            "source": "detector-suite-v0.2-exact-search-grid",
            "window_duration_ms": 8,
            "nominal_stride_seconds": 5,
            "maximum_probe_count": 8,
            "surrogate_count": 4,
            "coverage_semantics": "stratified-temporal-sampling-not-full-dwell",
            "decision_semantics": "candidate-evidence-not-calibrated-detection",
            "requested_output_schema": {
                "schema_id": "org.leo-flow.starlink-temporal-pilot-recording-bundle",
                "version": "0.1",
            },
        },
        "model": {
            "algorithm_ref": _artifact_document(MODEL_ALGORITHM_REF),
            "config_ref": _artifact_document(MODEL_CONFIG_REF),
        },
        "execution": {
            "environment_ref": _artifact_document(ENVIRONMENT_REF),
            "dependency_lock_ref": _artifact_document(DEPENDENCY_LOCK_REF),
            "recording_producer": "leo-flow-gauss-quality-psd/0.1.0",
            "waterfall_producer": "leo-flow-gauss-waterfall/0.1.0",
            "starlink_producer": (
                "leo-flow-gauss-starlink-known-code-candidates/0.1.0"
            ),
            "starlink_suite_producer": ("leo-flow-gauss-starlink-detector-suite/0.2.0"),
            "starlink_pilot_constellation_producer": (
                "leo-flow-gauss-starlink-published-pilot-constellation/0.1.0"
            ),
            "starlink_temporal_pilot_producer": (
                "leo-flow-gauss-starlink-stratified-temporal-pilot/0.1.0"
            ),
            "model_producer": ("leo-flow-gauss-receiver-quality-aggregate/0.1.0"),
            "host_class": "gauss-x86_64-python31116",
            "python": APPROVED_PYTHON_VERSION,
            "source_commit_utc_ns": int(SOURCE_COMMIT_UTC_NS),
        },
    }


SCIENCE_MANIFEST_DIGEST: Final[Digest] = canonical_digest(science_manifest())
