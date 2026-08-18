from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from playwright.sync_api import expect, sync_playwright

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.contracts.core import RecordingId
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV3,
    DashboardJsonApplicationV16,
    DashboardJsonApplicationV17,
    DashboardJsonApplicationV18,
)
from leo_flow.dashboard.ui import DashboardUiApplication
from tests.dashboard._fixtures import repository


class _MasterEvidencePorts:
    def recording_capture_detail(self, recording_id: RecordingId) -> dict[str, object]:
        return {
            "recording_id": recording_id,
            "capture_started_utc_ns": 1_000_000_000,
            "capture_finished_utc_ns": 61_000_000_000,
        }

    def recording_waterfall(self, recording_id: RecordingId) -> dict[str, object]:
        raise LookupError(recording_id)

    def recording_starlink_decision(
        self, recording_id: RecordingId
    ) -> dict[str, object]:
        raise LookupError(recording_id)

    def recording_starlink_acquired_constellation(
        self, query: Any
    ) -> dict[str, object]:
        recording_id = str(query.recording_id)
        return {
            "recording_id": recording_id,
            "analysis_ref": {"artifact_id": f"slqam3rec_{recording_id}"},
            "candidate_only": True,
            "calibration_required": True,
            "mode": "overall",
            "streams": [
                {
                    "radio_id": "radio_a",
                    "lnb_id": "lnb-current-01",
                    "receiver_chain_id": "rx_current_01",
                    "segment_id": "seg_current_01",
                    "edge": "lower",
                    "overall": {
                        "support_weighted_hard_symbol_accuracy": 0.80,
                        "support_weighted_rms_evm": 0.78,
                        "window_count": 32,
                    },
                    "windows": [],
                }
            ],
        }

    def capture_doppler_summaries(self, query: Any) -> dict[str, object]:
        del query
        return {
            "candidate_only": True,
            "calibrated_detection_count": None,
            "warnings": ["radio-lnb-receiver-candidates-are-never-pooled"],
            "recordings": [
                {
                    "recording_id": "rec_ready_a",
                    "state": "complete",
                    "candidates": [
                        {
                            "lnb_id": "lnb-current-01",
                            "receiver_chain_id": "rx_current_01",
                            "drift_rate_hz_s": 4_250.0,
                            "candidate_id": "candidate-current-01",
                            "model": "linear",
                            "ranking_score": 0.91,
                            "doppler_id": "doppler-current-01",
                            "algorithm_version": "0.1.0",
                        }
                    ],
                    "reason_codes": [],
                }
            ],
        }


@contextmanager
def _running_master_dashboard() -> Iterator[str]:
    queries = repository(50)
    ports = _MasterEvidencePorts()
    v3 = DashboardJsonApplicationV3(queries, queries, ports, ports, ports)
    v17 = DashboardJsonApplicationV17(cast(DashboardJsonApplicationV16, v3), ports)
    app = DashboardUiApplication(DashboardJsonApplicationV18(v17, ports))
    server = StdlibDashboardServer(request_timeout_s=0.01)
    server.preflight("127.0.0.1", 0)
    stopped = threading.Event()

    def serve() -> None:
        while not stopped.is_set():
            server.serve_once(app)

    worker = threading.Thread(target=serve, name="master-evidence-e2e")
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.bound_port}"
    finally:
        stopped.set()
        worker.join(timeout=1)
        server.close(1)
        assert not worker.is_alive()


def _browser_environment() -> dict[str, str | float | bool]:
    environment: dict[str, str | float | bool] = dict(os.environ)
    root = Path.home() / ".cache" / "ms-playwright" / "ubuntu-libs"
    if root.is_dir():
        environment["LD_LIBRARY_PATH"] = str(root / "usr/lib/x86_64-linux-gnu")
        environment["FONTCONFIG_FILE"] = str(root / "etc/fonts/fonts.conf")
        environment["FONTCONFIG_SYSROOT"] = str(root)
    return environment


def test_master_capture_table_populates_every_evidence_and_navigation_field() -> None:
    with _running_master_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=_browser_environment())
        try:
            page = browser.new_page()
            page.add_init_script(
                """
                (() => {
                  const NativeDate = Date;
                  class FixedDate extends NativeDate {
                        constructor(...args) { super(...(args.length ? args : [7200000])); }
                        static now() { return 7200000; }
                  }
                  FixedDate.parse = NativeDate.parse;
                  FixedDate.UTC = NativeDate.UTC;
                  globalThis.Date = FixedDate;
                })();
                """
            )
            response = page.goto(base_url)
            assert response is not None and response.ok

            headers = page.locator("#capture-batches-table thead th")
            expect(headers).to_have_count(9)
            expect(headers).to_have_text(
                [
                    "UTC",
                    "Radio",
                    "Capture",
                    "Analysis",
                    "Measured Dopplercandidate-only total fit",
                    "QAM goodnesscandidate-only / LNB / RX",
                    "Pilot beaconsA8 / GLRT",
                    "Capture time",
                    "Satellites tracked",
                ]
            )

            row = page.locator('[data-attempt-id="cattempt_ready_a"]')
            expect(row.locator("th, td")).to_have_count(9)
            expect(row.locator("th")).to_contain_text("00:00:00")
            expect(row.locator("td").nth(0)).to_have_text("radio_a")
            expect(row.locator(".capture-status-icon")).to_have_attribute(
                "data-state", "succeeded"
            )
            expect(row.locator(".analysis-status-icon")).to_have_attribute(
                "data-state", "complete"
            )
            doppler = row.locator(".capture-doppler-summary")
            expect(doppler).to_have_attribute("data-state", "complete")
            expect(doppler).to_contain_text(
                "lnb-current-01 / rx_current_01: 4.25 kHz/s"
            )
            qam = row.locator(".capture-qam-summary")
            expect(qam).to_have_attribute("data-state", "complete")
            expect(qam).to_contain_text("lnb-current-01 / rx_current_01:")
            expect(qam).to_contain_text("high")
            expect(qam.locator(".qam-detail-link")).to_have_attribute(
                "href", "/recordings/rec_ready_a#evidence-qam"
            )
            expect(row.locator(".pilot-detection-counts")).to_have_text("— / —")
            expect(row.locator(".pilot-detection-counts")).to_have_attribute(
                "title",
                "Calibrated Anchor-8 and GLRT beacon detections are unavailable",
            )
            expect(row.locator(".capture-duration")).to_have_text(
                "60.00 s", timeout=15_000
            )
            expect(row.locator("td").nth(7)).to_have_text("—")
            expect(row.locator("td").nth(7)).to_have_attribute(
                "title",
                "Recording-to-satellite association is not available in the dashboard contract",
            )
            expect(row).to_have_attribute(
                "aria-label",
                "View capture details, waterfall, and analysis for rec_ready_a",
            )
        finally:
            browser.close()
