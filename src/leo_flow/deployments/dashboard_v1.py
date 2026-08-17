"""Dashboard v1 deployment: loopback HTTP over read-only PostgreSQL queries.

Importing this module builds immutable Python registries only. PostgreSQL is an
optional server dependency and is imported lazily when this exact query adapter
is selected during bootstrap. No database connection or socket bind occurs
until the assembled service enters preflight or handles a request.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, cast

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.contracts.dashboard_batch import CaptureBatchDashboardQueryPortV0_1
from leo_flow.contracts.dashboard_observation import ObservationAggregateQueryPortV0_1
from leo_flow.contracts.dashboard_recording import (
    RecordingCaptureDetailQueryPortV0_1,
)
from leo_flow.contracts.dashboard_waterfall import RecordingWaterfallQueryPortV0_1
from leo_flow.contracts.ports import DashboardQueryPort
from leo_flow.contracts.radio_lifecycle import CaptureLifecycleDashboardQueryPortV0_1
from leo_flow.contracts.starlink_pipeline import RecordingStarlinkDecisionQueryPortV0_1
from leo_flow.contracts.starlink_suite_pipeline import (
    RecordingStarlinkSuiteQueryPortV0_2,
)
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV3,
    DashboardJsonApplicationV4,
    DashboardJsonApplicationV5,
    DashboardJsonApplicationV6,
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
_POSTGRES_TIMEOUT_S = 5


class DashboardRuntimeDependencyError(RuntimeError):
    """The selected dashboard runtime dependency is unavailable."""


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
    Protocol,
):
    """Exact Release B dashboard read surface, including detector-suite v0.2."""


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


def _postgres_query_projection(context: AdapterBuildContext) -> DashboardV4QueryPort:
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

    return PostgresDashboardRepository(connect)


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
    queries = cast(DashboardV4QueryPort, adapters[Capability.QUERY_PROJECTION])
    server = cast(ReadOnlyDashboardServer, adapters[Capability.DASHBOARD_SERVER])
    readiness_checked_server = _ReadinessCheckedDashboardServer(
        server,
        queries,
        cleanup_timeout_s=config.runtime.shutdown_timeout_s,
    )
    return build_dashboard_service(
        config,
        readiness_checked_server,
        DashboardUiApplication(
            DashboardJsonApplicationV6(
                DashboardJsonApplicationV5(
                    DashboardJsonApplicationV4(
                        DashboardJsonApplicationV3(
                            queries, queries, queries, queries, queries
                        ),
                        queries,
                    ),
                    queries,
                ),
                queries,
            ),
        ),
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
