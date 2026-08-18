"""Dashboard v1 deployment: loopback HTTP over read-only PostgreSQL queries.

Importing this module builds immutable Python registries only. PostgreSQL is an
optional server dependency and is imported lazily when this exact query adapter
is selected during bootstrap. No database connection or socket bind occurs
until the assembled service enters preflight or handles a request.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, cast

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.contracts.dashboard_advanced_doppler import (
    RecordingEvidenceAdvancedDopplerQueryPortV0_1,
)
from leo_flow.contracts.dashboard_batch import CaptureBatchDashboardQueryPortV0_1
from leo_flow.contracts.dashboard_capture_doppler import (
    CaptureDopplerSummaryQueryPortV0_1,
)
from leo_flow.contracts.dashboard_capture_qam import CaptureQamSummaryQueryPortV0_1
from leo_flow.contracts.dashboard_doppler import (
    RecordingDopplerVisualizationQueryPortV0_1,
)
from leo_flow.contracts.dashboard_doppler_aggregate import (
    DopplerAggregateQueryPortV0_1,
)
from leo_flow.contracts.dashboard_full_dwell_timeline import (
    RecordingFullDwellTimelineQueryPortV0_1,
)
from leo_flow.contracts.dashboard_observation import ObservationAggregateQueryPortV0_1
from leo_flow.contracts.dashboard_pilot_doppler import (
    RecordingPilotDopplerAssociationQueryPortV0_1,
)
from leo_flow.contracts.dashboard_recording import (
    RecordingCaptureDetailQueryPortV0_1,
)
from leo_flow.contracts.dashboard_recording_analysis_approach import (
    RecordingAnalysisApproachQueryPortV0_1,
)
from leo_flow.contracts.dashboard_recording_evidence import (
    RecordingEvidenceContextQueryPortV0_1,
    RecordingEvidenceDopplerQueryPortV0_1,
)
from leo_flow.contracts.dashboard_retro_qam_canary import (
    RetroQamCanaryDashboardQueryPortV0_1,
    RetroQamCanaryDashboardViewV0_1,
)
from leo_flow.contracts.dashboard_score_distribution import (
    PointScoreDistributionQueryPortV0_2,
    ScoreDistributionQueryPortV0_1,
)
from leo_flow.contracts.dashboard_surrogate_distribution import (
    SurrogateScoreDistributionQueryPortV0_1,
)
from leo_flow.contracts.dashboard_symbolwise_replay import (
    RecordingSymbolwiseReplayDashboardQueryPortV0_1,
)
from leo_flow.contracts.dashboard_temporal_pilot import (
    TemporalPilotAggregateQueryPortV0_1,
)
from leo_flow.contracts.dashboard_waterfall import RecordingWaterfallQueryPortV0_1
from leo_flow.contracts.ports import DashboardQueryPort
from leo_flow.contracts.radio_lifecycle import CaptureLifecycleDashboardQueryPortV0_1
from leo_flow.contracts.starlink_acquired_constellation_pipeline import (
    RecordingStarlinkAcquiredConstellationQueryPortV0_3,
)
from leo_flow.contracts.starlink_adaptive_qam import (
    RecordingStarlinkAdaptiveQamQueryPortV0_4,
)
from leo_flow.contracts.starlink_adaptive_response import (
    RecordingStarlinkAdaptiveResponseQueryPortV0_1,
)
from leo_flow.contracts.starlink_full_dwell_response import (
    RecordingStarlinkFullDwellQueryPortV0_1,
)
from leo_flow.contracts.starlink_pilot_constellation_pipeline import (
    RecordingStarlinkPilotConstellationQueryPortV0_1,
)
from leo_flow.contracts.starlink_pilot_prescreen import (
    RecordingStarlinkPilotPrescreenQueryPortV0_1,
)
from leo_flow.contracts.starlink_pilot_refinement import (
    RecordingStarlinkPilotRefinementQueryPortV0_1,
)
from leo_flow.contracts.starlink_pipeline import RecordingStarlinkDecisionQueryPortV0_1
from leo_flow.contracts.starlink_suite_pipeline import (
    RecordingStarlinkSuiteQueryPortV0_2,
)
from leo_flow.contracts.starlink_surrogate_null_pipeline import (
    RecordingStarlinkSurrogateNullQueryPortV0_1,
)
from leo_flow.contracts.starlink_temporal_pilot import (
    RecordingStarlinkTemporalPilotQueryPortV0_1,
)
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV3,
    DashboardJsonApplicationV4,
    DashboardJsonApplicationV5,
    DashboardJsonApplicationV6,
    DashboardJsonApplicationV7,
    DashboardJsonApplicationV8,
    DashboardJsonApplicationV9,
    DashboardJsonApplicationV10,
    DashboardJsonApplicationV11,
    DashboardJsonApplicationV12,
    DashboardJsonApplicationV13,
    DashboardJsonApplicationV14,
    DashboardJsonApplicationV15,
    DashboardJsonApplicationV16,
    DashboardJsonApplicationV17,
    DashboardJsonApplicationV18,
    DashboardJsonApplicationV19,
    DashboardJsonApplicationV20,
    DashboardJsonApplicationV21,
    DashboardJsonApplicationV22,
    DashboardJsonApplicationV23,
    DashboardJsonApplicationV24,
    DashboardJsonApplicationV25,
    DashboardJsonApplicationV26,
    DashboardJsonApplicationV27,
    DashboardJsonApplicationV28,
    DashboardJsonApplicationV29,
    JsonDashboardHandler,
)
from leo_flow.dashboard.ui import DashboardUiApplication
from leo_flow.services.bootstrap import (
    AdapterBuildContext,
    AdapterManifest,
    AdapterSet,
    Capability,
    DeploymentPlugin,
    Process,
)
from leo_flow.services.config import DashboardServiceConfig, SecretRef, ServiceConfig
from leo_flow.services.dashboard import ReadOnlyDashboardServer, build_dashboard_service
from leo_flow.services.lifecycle import DiagnosticSink, ServiceLoop

if TYPE_CHECKING:
    from psycopg import Connection

QUERY_PROJECTION_REF = "dashboard.postgres-projection-v1"
SERVER_REF = "dashboard.stdlib-loopback-http-v1"
REMOTE_SERVER_REF = "dashboard.stdlib-explicit-remote-http-v1"
SECRET_PROVIDER = "systemd-credential"
DATABASE_SECRET = SecretRef(SECRET_PROVIDER, "catalog-dsn")
CAS_ROOT_SECRET = SecretRef(SECRET_PROVIDER, "analysis-cas-root")
_POSTGRES_TIMEOUT_S = 5


class DashboardRuntimeDependencyError(RuntimeError):
    """The selected dashboard runtime dependency is unavailable."""


class _UnavailableRetroQamCanary:
    def latest_retro_qam_canary(self) -> RetroQamCanaryDashboardViewV0_1:
        raise LookupError("historical QAM canary receipt is unavailable")


class DashboardV3QueryPort(
    DashboardQueryPort,
    CaptureBatchDashboardQueryPortV0_1,
    RecordingCaptureDetailQueryPortV0_1,
    RecordingWaterfallQueryPortV0_1,
    RecordingStarlinkDecisionQueryPortV0_1,
    Protocol,
):
    """Deployment composition of independently versioned read ports."""


class DashboardV4QueryPort(
    DashboardV3QueryPort,
    RecordingStarlinkSuiteQueryPortV0_2,
    CaptureLifecycleDashboardQueryPortV0_1,
    ObservationAggregateQueryPortV0_1,
    ScoreDistributionQueryPortV0_1,
    PointScoreDistributionQueryPortV0_2,
    Protocol,
):
    """Exact Release B dashboard read surface, including detector-suite v0.2."""


class DashboardV9QueryPort(
    DashboardV4QueryPort,
    RecordingDopplerVisualizationQueryPortV0_1,
    Protocol,
):
    """Additive Doppler visualization read surface."""


class DashboardV10QueryPort(
    DashboardV9QueryPort,
    RecordingStarlinkSurrogateNullQueryPortV0_1,
    Protocol,
):
    """Additive paired-surrogate evidence read surface."""


class DashboardV11QueryPort(
    DashboardV10QueryPort,
    RecordingStarlinkPilotConstellationQueryPortV0_1,
    Protocol,
):
    """Additive published edge-pilot constellation read surface."""


class DashboardV12QueryPort(
    DashboardV11QueryPort,
    SurrogateScoreDistributionQueryPortV0_1,
    Protocol,
):
    """Additive aggregate Qin-versus-surrogate read surface."""


class DashboardV13QueryPort(
    DashboardV12QueryPort,
    RecordingStarlinkTemporalPilotQueryPortV0_1,
    TemporalPilotAggregateQueryPortV0_1,
    Protocol,
):
    """Additive stratified temporal pilot read surface."""


class DashboardV14QueryPort(
    DashboardV13QueryPort,
    DopplerAggregateQueryPortV0_1,
    Protocol,
):
    """Additive candidate-only aggregate Doppler read surface."""


class DashboardV15QueryPort(
    DashboardV14QueryPort,
    RecordingStarlinkFullDwellQueryPortV0_1,
    Protocol,
):
    """Additive sparse-exact full-dwell read surface."""


class DashboardV16QueryPort(
    DashboardV15QueryPort,
    RecordingEvidenceContextQueryPortV0_1,
    RecordingEvidenceDopplerQueryPortV0_1,
    Protocol,
):
    """Additive authoritative selector context for recording evidence."""


class DashboardV17QueryPort(
    DashboardV16QueryPort,
    RecordingStarlinkAcquiredConstellationQueryPortV0_3,
    Protocol,
):
    """Additive durable overall/windowed acquired-QAM read surface."""


class DashboardV18QueryPort(
    DashboardV17QueryPort,
    CaptureDopplerSummaryQueryPortV0_1,
    Protocol,
):
    """Additive bounded master-table Doppler summary surface."""


class DashboardV19QueryPort(
    DashboardV18QueryPort,
    RecordingEvidenceAdvancedDopplerQueryPortV0_1,
    Protocol,
):
    """Additive exact advanced-path-only Doppler read surface."""


class DashboardV20QueryPort(
    DashboardV19QueryPort,
    RecordingFullDwellTimelineQueryPortV0_1,
    Protocol,
):
    """Additive complete full-dwell prescreen timeline read surface."""


class DashboardV22QueryPort(
    DashboardV20QueryPort,
    CaptureQamSummaryQueryPortV0_1,
    Protocol,
):
    """Additive bounded master-table QAM-goodness read surface."""


class DashboardV23QueryPort(
    DashboardV22QueryPort,
    RecordingAnalysisApproachQueryPortV0_1,
    Protocol,
):
    """Add exact recording-level analysis/window/search facts."""


class DashboardV24QueryPort(
    DashboardV23QueryPort,
    RecordingStarlinkAdaptiveResponseQueryPortV0_1,
    Protocol,
):
    """Add bounded pattern-symmetric adaptive response traces."""


class DashboardV25QueryPort(
    DashboardV24QueryPort,
    RecordingStarlinkAdaptiveQamQueryPortV0_4,
    Protocol,
):
    """Add adaptively selected acquired-QAM windows."""


class DashboardV26QueryPort(
    DashboardV25QueryPort,
    RecordingPilotDopplerAssociationQueryPortV0_1,
    Protocol,
):
    """Add pilot-frequency association for blind Doppler paths."""


class DashboardV27QueryPort(
    DashboardV26QueryPort,
    RecordingStarlinkPilotPrescreenQueryPortV0_1,
    Protocol,
):
    """Add complete-IQ pattern-blind pilot-prescreen timelines."""


class DashboardV28QueryPort(
    DashboardV27QueryPort,
    RecordingStarlinkPilotRefinementQueryPortV0_1,
    Protocol,
):
    """Add exact Qin/surrogate responses at complete-IQ prescreen seeds."""


class DashboardV29QueryPort(
    DashboardV28QueryPort,
    RecordingSymbolwiseReplayDashboardQueryPortV0_1,
    Protocol,
):
    """Add complete durable symbolwise replay curves with hardware context."""


class _ReadinessCheckedDashboardServer:
    """Bind and prove the query capability before reporting process readiness."""

    def __init__(
        self,
        server: ReadOnlyDashboardServer,
        queries: DashboardQueryPort,
        *,
        cleanup_timeout_s: float,
    ) -> None:
        self._server = server
        self._queries = queries
        self._cleanup_timeout_s = cleanup_timeout_s

    def preflight(self, bind_host: str, bind_port: int) -> None:
        self._server.preflight(bind_host, bind_port)
        try:
            self._queries.storage_health()
        except Exception:
            self._server.close(self._cleanup_timeout_s)
            raise

    def serve_once(self, handler: JsonDashboardHandler) -> bool:
        return self._server.serve_once(handler)

    def close(self, timeout_s: float) -> None:
        self._server.close(timeout_s)


def _postgres_query_projection(context: AdapterBuildContext) -> DashboardV29QueryPort:
    try:
        dsn = context.secrets[DATABASE_SECRET]
    except KeyError as error:
        raise ValueError("catalog database credential was not configured") from error
    try:
        import psycopg
        from psycopg.rows import dict_row

        from leo_flow.adapters.dashboard_postgres import PostgresDashboardRepository
    except ImportError as error:
        raise DashboardRuntimeDependencyError(
            "dashboard PostgreSQL support requires the 'server' optional dependency"
        ) from error

    def connect() -> Connection[dict[str, object]]:
        connection = psycopg.connect(
            dsn,
            row_factory=dict_row,
            connect_timeout=_POSTGRES_TIMEOUT_S,
            options=(
                f"-c statement_timeout={_POSTGRES_TIMEOUT_S * 1000} "
                f"-c lock_timeout={_POSTGRES_TIMEOUT_S * 1000}"
            ),
        )
        connection.execute("SET ROLE leo_dashboard")
        return connection

    doppler = None
    surrogate_nulls = None
    pilot_constellations = None
    surrogate_distributions = None
    temporal_pilots = None
    temporal_aggregate = None
    doppler_aggregate = None
    full_dwell = None
    full_dwell_timeline = None
    acquired_qam = None
    adaptive_responses = None
    adaptive_qam = None
    pilot_prescreens = None
    pilot_refinements = None
    symbolwise_replays = None
    cas_root = context.secrets.get(CAS_ROOT_SECRET)
    if cas_root is not None:
        from leo_flow.adapters.dashboard_doppler_projection import (
            DurableDashboardDopplerProjectionV0_1,
        )
        from leo_flow.adapters.starlink_pilot_constellation_postgres import (
            PostgresStarlinkPilotConstellationCatalogV0_1,
        )
        from leo_flow.adapters.starlink_surrogate_null_postgres import (
            PostgresStarlinkSurrogateNullCatalogV0_1,
        )
        from leo_flow.adapters.waterfall_doppler_postgres import (
            PostgresWaterfallDopplerQueryV0_1,
        )
        from leo_flow.analysis.recording.starlink_pilot_constellation_persistence import (
            DurableRecordingStarlinkPilotConstellationQueryV0_1,
            DurableStarlinkPilotConstellationStoreV0_1,
            StarlinkPilotConstellationBlobStore,
        )
        from leo_flow.analysis.recording.starlink_surrogate_null_persistence import (
            DurableRecordingStarlinkSurrogateNullQueryV0_1,
            DurableStarlinkSurrogateNullStoreV0_1,
            StarlinkSurrogateNullBlobStore,
        )
        from leo_flow.analysis.recording.starlink_temporal_pilot_persistence import (
            DurableRecordingStarlinkTemporalPilotQueryV0_1,
            DurableStarlinkTemporalPilotStoreV0_1,
            StarlinkTemporalPilotBlobStore,
        )
        from leo_flow.analysis.recording.waterfall_v0_2_persistence import (
            DurableWaterfallReaderV0_2,
        )
        from leo_flow.analysis.tracking.doppler_persistence import (
            DurableDopplerReaderV0_1,
        )
        from leo_flow.storage.filesystem import FileSystemBlobReader

        query = PostgresWaterfallDopplerQueryV0_1(connect)
        blobs = FileSystemBlobReader(cas_root)
        doppler = DurableDashboardDopplerProjectionV0_1(
            query,
            DurableWaterfallReaderV0_2(blobs, query),
            DurableDopplerReaderV0_1(blobs, query),
        )
        surrogate_catalog = PostgresStarlinkSurrogateNullCatalogV0_1(connect)
        surrogate_store = DurableStarlinkSurrogateNullStoreV0_1(
            cast(StarlinkSurrogateNullBlobStore, blobs),
            surrogate_catalog,
        )
        surrogate_nulls = DurableRecordingStarlinkSurrogateNullQueryV0_1(
            surrogate_store,
            surrogate_catalog,
        )
        constellation_catalog = PostgresStarlinkPilotConstellationCatalogV0_1(connect)
        constellation_store = DurableStarlinkPilotConstellationStoreV0_1(
            cast(StarlinkPilotConstellationBlobStore, blobs),
            constellation_catalog,
        )
        pilot_constellations = DurableRecordingStarlinkPilotConstellationQueryV0_1(
            constellation_store,
            constellation_catalog,
        )
        from leo_flow.adapters.dashboard_surrogate_distribution import (
            DurableDashboardSurrogateDistributionV0_1,
        )

        surrogate_distributions = DurableDashboardSurrogateDistributionV0_1(
            connect, blobs
        )
        from leo_flow.adapters.dashboard_doppler_aggregate import (
            DurableDashboardDopplerAggregateV0_1,
        )

        doppler_aggregate = DurableDashboardDopplerAggregateV0_1(connect, blobs)
        from leo_flow.adapters.starlink_temporal_pilot_postgres import (
            PostgresStarlinkTemporalPilotCatalogV0_1,
        )

        temporal_catalog = PostgresStarlinkTemporalPilotCatalogV0_1(connect)
        temporal_pilots = DurableRecordingStarlinkTemporalPilotQueryV0_1(
            DurableStarlinkTemporalPilotStoreV0_1(
                cast(StarlinkTemporalPilotBlobStore, blobs), temporal_catalog
            ),
            temporal_catalog,
        )
        from leo_flow.adapters.dashboard_temporal_pilot import (
            DurableDashboardTemporalPilotAggregateV0_1,
        )

        temporal_aggregate = DurableDashboardTemporalPilotAggregateV0_1(connect, blobs)
        from leo_flow.adapters.full_dwell_timeline_postgres import (
            PostgresFullDwellTimelineCatalogV0_1,
        )
        from leo_flow.adapters.starlink_full_dwell_postgres import (
            PostgresStarlinkFullDwellCatalogV0_1,
        )
        from leo_flow.analysis.recording.starlink_full_dwell_response_persistence import (
            DurableRecordingStarlinkFullDwellQueryV0_1,
            DurableStarlinkFullDwellStoreV0_1,
            StarlinkFullDwellBlobStore,
        )
        from leo_flow.analysis.recording.starlink_full_dwell_timeline import (
            DurableRecordingFullDwellTimelineQueryV0_1,
            DurableRecordingPromptFullDwellTimelineQueryV0_1,
            PreferPromptFullDwellTimelineQueryV0_1,
        )
        from leo_flow.analysis.recording.starlink_full_dwell_timeline_persistence import (
            DurableFullDwellTimelineStoreV0_1,
            FullDwellTimelineBlobStore,
        )

        full_dwell_catalog = PostgresStarlinkFullDwellCatalogV0_1(connect)
        full_dwell_store = DurableStarlinkFullDwellStoreV0_1(
            cast(StarlinkFullDwellBlobStore, blobs), full_dwell_catalog
        )
        full_dwell = DurableRecordingStarlinkFullDwellQueryV0_1(
            full_dwell_store, full_dwell_catalog
        )
        legacy_full_dwell_timeline = DurableRecordingFullDwellTimelineQueryV0_1(
            full_dwell_store, full_dwell_catalog
        )
        prompt_timeline_catalog = PostgresFullDwellTimelineCatalogV0_1(connect)
        prompt_full_dwell_timeline = DurableRecordingPromptFullDwellTimelineQueryV0_1(
            DurableFullDwellTimelineStoreV0_1(
                cast(FullDwellTimelineBlobStore, blobs), prompt_timeline_catalog
            ),
            prompt_timeline_catalog,
        )
        full_dwell_timeline = PreferPromptFullDwellTimelineQueryV0_1(
            prompt_full_dwell_timeline, legacy_full_dwell_timeline
        )
        from leo_flow.adapters.starlink_acquired_constellation_postgres import (
            PostgresRecordingReceiverLnbResolverV0_3,
            PostgresStarlinkAcquiredConstellationCatalogV0_3,
        )
        from leo_flow.analysis.recording.starlink_acquired_constellation_persistence import (
            DurableRecordingStarlinkAcquiredConstellationQueryV0_3,
            DurableStarlinkAcquiredConstellationStoreV0_3,
            StarlinkAcquiredConstellationBlobStore,
        )

        acquired_catalog = PostgresStarlinkAcquiredConstellationCatalogV0_3(connect)
        acquired_qam = DurableRecordingStarlinkAcquiredConstellationQueryV0_3(
            DurableStarlinkAcquiredConstellationStoreV0_3(
                cast(StarlinkAcquiredConstellationBlobStore, blobs), acquired_catalog
            ),
            acquired_catalog,
            PostgresRecordingReceiverLnbResolverV0_3(connect),
        )
        from leo_flow.adapters.starlink_adaptive_response_postgres import (
            PostgresStarlinkAdaptiveResponseCatalogV0_1,
        )
        from leo_flow.analysis.recording.starlink_adaptive_response_persistence import (
            DurableRecordingStarlinkAdaptiveResponseQueryV0_1,
            DurableStarlinkAdaptiveResponseStoreV0_1,
            StarlinkAdaptiveResponseBlobStore,
        )

        adaptive_catalog = PostgresStarlinkAdaptiveResponseCatalogV0_1(connect)
        adaptive_responses = DurableRecordingStarlinkAdaptiveResponseQueryV0_1(
            DurableStarlinkAdaptiveResponseStoreV0_1(
                cast(StarlinkAdaptiveResponseBlobStore, blobs), adaptive_catalog
            ),
            adaptive_catalog,
        )
        from leo_flow.adapters.starlink_adaptive_qam_postgres import (
            PostgresStarlinkAdaptiveQamCatalogV0_4,
        )
        from leo_flow.analysis.recording.starlink_adaptive_qam_persistence import (
            DurableRecordingStarlinkAdaptiveQamQueryV0_4,
            DurableStarlinkAdaptiveQamStoreV0_4,
            StarlinkAdaptiveQamBlobStore,
        )

        adaptive_qam_catalog = PostgresStarlinkAdaptiveQamCatalogV0_4(connect)
        adaptive_qam = DurableRecordingStarlinkAdaptiveQamQueryV0_4(
            DurableStarlinkAdaptiveQamStoreV0_4(
                cast(StarlinkAdaptiveQamBlobStore, blobs), adaptive_qam_catalog
            ),
            adaptive_qam_catalog,
        )
        from leo_flow.adapters.starlink_pilot_prescreen_postgres import (
            PostgresStarlinkPilotPrescreenCatalogV0_1,
        )
        from leo_flow.analysis.recording.starlink_pilot_prescreen_persistence import (
            DurableRecordingStarlinkPilotPrescreenQueryV0_1,
            DurableStarlinkPilotPrescreenStoreV0_1,
            StarlinkPilotPrescreenBlobStore,
        )

        pilot_prescreen_catalog = PostgresStarlinkPilotPrescreenCatalogV0_1(connect)
        pilot_prescreens = DurableRecordingStarlinkPilotPrescreenQueryV0_1(
            DurableStarlinkPilotPrescreenStoreV0_1(
                cast(StarlinkPilotPrescreenBlobStore, blobs), pilot_prescreen_catalog
            ),
            pilot_prescreen_catalog,
        )
        from leo_flow.adapters.starlink_pilot_refinement_postgres import (
            PostgresStarlinkPilotRefinementCatalogV0_1,
        )
        from leo_flow.analysis.recording.starlink_pilot_refinement_persistence import (
            DurableRecordingStarlinkPilotRefinementQueryV0_1,
            DurableStarlinkPilotRefinementStoreV0_1,
            StarlinkPilotRefinementBlobStore,
        )

        pilot_refinement_catalog = PostgresStarlinkPilotRefinementCatalogV0_1(connect)
        pilot_refinements = DurableRecordingStarlinkPilotRefinementQueryV0_1(
            DurableStarlinkPilotRefinementStoreV0_1(
                cast(StarlinkPilotRefinementBlobStore, blobs),
                pilot_refinement_catalog,
            ),
            pilot_refinement_catalog,
        )
        from leo_flow.adapters.starlink_symbolwise_replay_postgres import (
            PostgresStarlinkSymbolwiseReplayRepositoryV0_1,
        )
        from leo_flow.analysis.recording.starlink_symbolwise_replay_product_persistence import (
            DurableRecordingStarlinkSymbolwiseReplayQueryV0_1,
            DurableStarlinkSymbolwiseReplayStoreV0_1,
            StarlinkSymbolwiseReplayBlobStore,
        )

        symbolwise_catalog = PostgresStarlinkSymbolwiseReplayRepositoryV0_1(connect)
        symbolwise_replays = DurableRecordingStarlinkSymbolwiseReplayQueryV0_1(
            DurableStarlinkSymbolwiseReplayStoreV0_1(
                cast(StarlinkSymbolwiseReplayBlobStore, blobs),
                symbolwise_catalog,
            ),
            symbolwise_catalog,
        )
    return PostgresDashboardRepository(
        connect,
        doppler=doppler,
        surrogate_nulls=surrogate_nulls,
        pilot_constellations=pilot_constellations,
        surrogate_distributions=surrogate_distributions,
        temporal_pilots=temporal_pilots,
        temporal_aggregate=temporal_aggregate,
        doppler_aggregate=doppler_aggregate,
        full_dwell=full_dwell,
        full_dwell_timeline=full_dwell_timeline,
        acquired_qam=acquired_qam,
        analysis_approaches=acquired_qam,
        adaptive_responses=adaptive_responses,
        adaptive_qam=adaptive_qam,
        pilot_prescreens=pilot_prescreens,
        pilot_refinements=pilot_refinements,
        symbolwise_replays=symbolwise_replays,
    )


def _stdlib_loopback_server(
    context: AdapterBuildContext,
) -> ReadOnlyDashboardServer:
    del context
    return StdlibDashboardServer()


def _stdlib_explicit_remote_server(
    context: AdapterBuildContext,
) -> ReadOnlyDashboardServer:
    del context
    return StdlibDashboardServer(allow_remote=True)


def _build_dashboard(
    config: ServiceConfig,
    adapters: AdapterSet,
    diagnostics: DiagnosticSink,
) -> ServiceLoop:
    if not isinstance(config, DashboardServiceConfig):
        raise TypeError("dashboard v1 requires dashboard configuration")
    queries = cast(DashboardV29QueryPort, adapters[Capability.QUERY_PROJECTION])
    server = cast(ReadOnlyDashboardServer, adapters[Capability.DASHBOARD_SERVER])
    readiness_checked_server = _ReadinessCheckedDashboardServer(
        server,
        queries,
        cleanup_timeout_s=config.runtime.shutdown_timeout_s,
    )
    v3 = DashboardJsonApplicationV3(queries, queries, queries, queries, queries)
    v4 = DashboardJsonApplicationV4(v3, queries)
    v5 = DashboardJsonApplicationV5(v4, queries)
    v6 = DashboardJsonApplicationV6(v5, queries)
    v7 = DashboardJsonApplicationV7(v6, queries)
    v8 = DashboardJsonApplicationV8(v7, queries)
    v9 = DashboardJsonApplicationV9(v8, queries)
    v10 = DashboardJsonApplicationV10(v9, queries)
    v11 = DashboardJsonApplicationV11(v10, queries)
    v12 = DashboardJsonApplicationV12(v11, queries)
    v13 = DashboardJsonApplicationV13(v12, queries, queries)
    v14 = DashboardJsonApplicationV14(v13, queries)
    v15 = DashboardJsonApplicationV15(v14, queries)
    v16 = DashboardJsonApplicationV16(v15, queries, queries)
    v17 = DashboardJsonApplicationV17(v16, queries)
    v18 = DashboardJsonApplicationV18(v17, queries)
    v19 = DashboardJsonApplicationV19(v18, queries)
    v20 = DashboardJsonApplicationV20(v19, queries)
    canary_path = os.environ.get("LEO_RETRO_QAM_CANARY_RECEIPT")
    canary: RetroQamCanaryDashboardQueryPortV0_1
    if canary_path is None:
        canary = _UnavailableRetroQamCanary()
    else:
        from leo_flow.adapters.dashboard_retro_qam_canary import (
            FileRetroQamCanaryDashboardQueryV0_1,
        )

        canary = FileRetroQamCanaryDashboardQueryV0_1(Path(canary_path))
    v21 = DashboardJsonApplicationV21(v20, canary)
    v22 = DashboardJsonApplicationV22(v21, queries)
    v23 = DashboardJsonApplicationV23(v22, queries)
    v24 = DashboardJsonApplicationV24(v23, queries)
    v25 = DashboardJsonApplicationV25(v24, queries)
    v26 = DashboardJsonApplicationV26(v25, queries)
    v27 = DashboardJsonApplicationV27(v26, queries)
    v28 = DashboardJsonApplicationV28(v27, queries)
    v29 = DashboardJsonApplicationV29(v28, queries)
    return build_dashboard_service(
        config,
        readiness_checked_server,
        DashboardUiApplication(v29),
        diagnostics=diagnostics,
    )


MANIFEST = AdapterManifest(
    {
        Process.DASHBOARD: {
            Capability.QUERY_PROJECTION: {
                QUERY_PROJECTION_REF: _postgres_query_projection
            },
            Capability.DASHBOARD_SERVER: {
                SERVER_REF: _stdlib_loopback_server,
                REMOTE_SERVER_REF: _stdlib_explicit_remote_server,
            },
        }
    }
)

PLUGIN = DeploymentPlugin(
    MANIFEST,
    MappingProxyType({SECRET_PROVIDER: SystemdCredentialProvider()}),
    MappingProxyType({Process.DASHBOARD: _build_dashboard}),
)
