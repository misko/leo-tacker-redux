"""Read-only dashboard process boundary over the public JSON handler."""

from __future__ import annotations

from typing import Protocol

from leo_flow.dashboard.api import JsonDashboardHandler

from .config import DashboardServiceConfig
from .lifecycle import DiagnosticSink, ServiceLoop


class ReadOnlyDashboardServer(Protocol):
    def preflight(self, bind_host: str, bind_port: int) -> None: ...

    def serve_once(self, handler: JsonDashboardHandler) -> bool: ...

    def close(self, timeout_s: float) -> None: ...


def build_dashboard_service(
    config: DashboardServiceConfig,
    server: ReadOnlyDashboardServer,
    handler: JsonDashboardHandler,
    *,
    diagnostics: DiagnosticSink | None = None,
) -> ServiceLoop:
    return ServiceLoop(
        service="dashboard",
        instance_id=config.runtime.instance_id,
        start=lambda: server.preflight(config.bind_host, config.bind_port),
        step=lambda: server.serve_once(handler),
        close=server.close,
        poll_interval_s=config.runtime.poll_interval_s,
        shutdown_timeout_s=config.runtime.shutdown_timeout_s,
        diagnostics=diagnostics,
    )
