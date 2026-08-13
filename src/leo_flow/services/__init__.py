"""Independent process composition surfaces."""

from .analysis import (
    AnalysisCycle,
    AnalysisJobProcessor,
    FencedAnalysisCycle,
    build_analysis_service,
)
from .capture import CaptureCycle, build_capture_service
from .config import (
    AnalysisServiceConfig,
    CaptureServiceConfig,
    ConfigurationError,
    DashboardServiceConfig,
    RuntimeConfig,
    SecretRef,
    ServiceConfig,
    load_service_config,
    parse_service_config,
)
from .dashboard import ReadOnlyDashboardServer, build_dashboard_service
from .lifecycle import (
    DiagnosticEvent,
    DiagnosticSink,
    HealthSnapshot,
    JsonLineDiagnosticSink,
    ServiceLifecycleError,
    ServiceLoop,
    ServiceState,
)

__all__ = [
    "AnalysisCycle",
    "AnalysisJobProcessor",
    "AnalysisServiceConfig",
    "CaptureCycle",
    "CaptureServiceConfig",
    "ConfigurationError",
    "DashboardServiceConfig",
    "DiagnosticEvent",
    "DiagnosticSink",
    "FencedAnalysisCycle",
    "HealthSnapshot",
    "JsonLineDiagnosticSink",
    "ReadOnlyDashboardServer",
    "RuntimeConfig",
    "SecretRef",
    "ServiceConfig",
    "ServiceLifecycleError",
    "ServiceLoop",
    "ServiceState",
    "build_analysis_service",
    "build_capture_service",
    "build_dashboard_service",
    "load_service_config",
    "parse_service_config",
]
