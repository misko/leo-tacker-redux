"""Capture-only service boundary; no analysis or dashboard dependency."""

from __future__ import annotations

from typing import Protocol

from .config import CaptureServiceConfig
from .lifecycle import DiagnosticSink, ServiceLoop


class CaptureCycle(Protocol):
    """Injected plan/capture/publication unit of work."""

    def preflight(self) -> None: ...

    def capture_and_publish_once(self) -> bool: ...

    def close(self, timeout_s: float) -> None: ...


def build_capture_service(
    config: CaptureServiceConfig,
    cycle: CaptureCycle,
    *,
    diagnostics: DiagnosticSink | None = None,
) -> ServiceLoop:
    return ServiceLoop(
        service="capture",
        instance_id=config.runtime.instance_id,
        start=cycle.preflight,
        step=cycle.capture_and_publish_once,
        close=cycle.close,
        poll_interval_s=config.runtime.poll_interval_s,
        shutdown_timeout_s=config.runtime.shutdown_timeout_s,
        diagnostics=diagnostics,
    )
