from __future__ import annotations

from dataclasses import dataclass

from leo_flow.services import (
    AnalysisServiceConfig,
    CaptureServiceConfig,
    DashboardServiceConfig,
    RuntimeConfig,
    build_analysis_service,
    build_capture_service,
    build_dashboard_service,
)

RUNTIME = RuntimeConfig("instance-1", 0.01, 0.1, ())


@dataclass
class Cycle:
    preflights: int = 0
    units: int = 0
    closes: int = 0

    def preflight(self, *args: object) -> None:
        self.preflights += 1

    def capture_and_publish_once(self) -> bool:
        self.units += 1
        return True

    def process_one_job(self) -> bool:
        self.units += 1
        return True

    def close(self, timeout_s: float) -> None:
        assert timeout_s == 0.1
        self.closes += 1


class DashboardServer(Cycle):
    def serve_once(self, handler: object) -> bool:
        assert handler is HANDLER
        self.units += 1
        return True


class Handler:
    def handle(self, request: object) -> object:
        raise AssertionError("not called by server fake")


HANDLER = Handler()


def test_capture_analysis_and_dashboard_are_independent_one_shot_processes() -> None:
    capture_cycle = Cycle()
    capture = build_capture_service(
        CaptureServiceConfig(
            1,
            "capture",
            RUNTIME,
            "plans",
            "radio",
            "preflight",
            "writer",
            "spool",
            "publisher",
        ),
        capture_cycle,
    )
    assert capture.run_once()
    capture.shutdown()

    analysis_cycle = Cycle()
    analysis = build_analysis_service(
        AnalysisServiceConfig(
            1, "analysis", RUNTIME, "jobs", "reader", "features", "models"
        ),
        analysis_cycle,
    )
    assert analysis.run_once()
    analysis.shutdown()

    dashboard_server = DashboardServer()
    dashboard = build_dashboard_service(
        DashboardServiceConfig(
            1, "dashboard", RUNTIME, "queries", "server", "127.0.0.1", 8080
        ),
        dashboard_server,
        HANDLER,
    )
    assert dashboard.run_once()
    dashboard.shutdown()

    assert capture_cycle == Cycle(1, 1, 1)
    assert analysis_cycle == Cycle(1, 1, 1)
    assert dashboard_server == DashboardServer(1, 1, 1)
