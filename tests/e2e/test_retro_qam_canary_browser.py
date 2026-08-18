from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.adapters.dashboard_retro_qam_canary import (
    FileRetroQamCanaryDashboardQueryV0_1,
)
from leo_flow.contracts.core import canonical_json_bytes
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV21,
    JsonRequest,
    JsonResponse,
)
from leo_flow.dashboard.ui import DashboardUiApplication
from tests.e2e.test_dashboard_browser import _browser_environment


class _MissingOtherDashboardProducts:
    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(
            404,
            (("content-type", "application/json; charset=utf-8"),),
            b'{"error":{"code":"not_found","message":"fixture product absent"}}',
        )


def _receipt() -> dict[str, object]:
    return {
        "schema": {
            "schema_id": "org.leo-flow.starlink-retro-qam-canary-receipt",
            "version": {"major": 0, "minor": 1},
        },
        "corpus_id": "retro-qam-2026-08-17-v1",
        "iq_object_digest": {"algorithm": "sha256", "value": "a" * 64},
        "git_commit": "935ef64",
        "completed_utc_ns": 1_787_026_764_761_437_071,
        "metrics_match_oracle": True,
        "candidate_only": True,
        "calibrated_detection": None,
        "reason_codes": [
            "known-published-pilot-regression",
            "candidate-evidence-not-calibrated-detection",
            "leo-tracker-oracle-not-runtime-dependency",
            "whole-input-sha256-verified-before-analysis",
        ],
        "receivers": [
            {
                "receiver_index": 0,
                "winning_epoch_sample": 2063,
                "winning_cfo_hz": 364134.65,
                "held_out_verify_score": 0.36,
                "conditioned_control_score": 0.02,
                "verify_minus_control_margin": 0.34,
                "hard_symbol_accuracy": 0.7479167,
                "rms_evm": 0.9426077,
            },
            {
                "receiver_index": 1,
                "winning_epoch_sample": 2063,
                "winning_cfo_hz": -194373.48,
                "held_out_verify_score": 0.35,
                "conditioned_control_score": 0.01,
                "verify_minus_control_margin": 0.34,
                "hard_symbol_accuracy": 0.7991667,
                "rms_evm": 0.7826342,
            },
        ],
        "combined": {
            "hard_symbol_accuracy": 0.8833333,
            "rms_evm": 0.63817147,
        },
    }


@contextmanager
def _running_dashboard(receipt_path: Path) -> Iterator[str]:
    server = StdlibDashboardServer(request_timeout_s=0.01)
    server.preflight("127.0.0.1", 0)
    application = DashboardUiApplication(
        DashboardJsonApplicationV21(
            _MissingOtherDashboardProducts(),
            FileRetroQamCanaryDashboardQueryV0_1(receipt_path),
        )
    )
    stopped = threading.Event()

    def serve() -> None:
        while not stopped.is_set():
            server.serve_once(application)

    worker = threading.Thread(target=serve, name="retro-qam-canary-browser-server")
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.bound_port}"
    finally:
        stopped.set()
        worker.join(timeout=1)
        server.close(1)
        assert not worker.is_alive()


def test_home_page_renders_latest_historical_qam_acceptance_canary(
    tmp_path: Path,
) -> None:
    receipt_path = tmp_path / "latest.receipt.json"
    receipt_path.write_bytes(canonical_json_bytes(_receipt()) + b"\n")
    with _running_dashboard(receipt_path) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=_browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1200})
            response = page.goto(base_url)
            assert response is not None and response.ok
            expect(page.locator("#retro-qam-canary-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#retro-qam-canary-state")).to_contain_text(
                "matches the frozen leo-tracker oracle"
            )
            expect(page.locator("#retro-qam-canary-metrics")).to_contain_text(
                "Oracle matchPASS"
            )
            expect(page.locator("#retro-qam-canary-metrics")).to_contain_text(
                "Combined accuracy88.33%"
            )
            expect(page.locator("#retro-qam-canary-body tr")).to_have_count(2)
            expect(page.locator("#retro-qam-canary-body")).to_contain_text("74.79%")
            expect(page.locator("#retro-qam-canary-body")).to_contain_text("79.92%")
            expect(page.locator("#retro-qam-canary-provenance")).to_contain_text(
                "Historical known-positive acceptance canary"
            )
        finally:
            browser.close()
