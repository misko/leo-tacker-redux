from __future__ import annotations

import os
import threading
import time
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
    DashboardJsonApplicationV22,
    JsonRequest,
    JsonResponse,
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
            "mode": query.mode.value,
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
                    "windows": [
                        {
                            "window_index": 0,
                            "hard_symbol_accuracy": 0.26,
                            "rms_evm": 4.0,
                            "verify_minus_control_margin": 0.001,
                        },
                        {
                            "window_index": 31,
                            "hard_symbol_accuracy": 0.88,
                            "rms_evm": 0.65,
                            "verify_minus_control_margin": 0.3,
                        },
                    ],
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

    def capture_qam_summaries(self, query: Any) -> dict[str, object]:
        assert query.maximum_recordings == 2
        return {
            "schema_version": 1,
            "start_utc_ns": query.start_utc_ns,
            "stop_utc_ns": query.stop_utc_ns,
            "candidate_only": True,
            "calibration_required": True,
            "calibrated_detection_count": None,
            "recordings": [
                {
                    "recording_id": "rec_ready_a",
                    "radio_id": "radio_a",
                    "analysis_state": "complete",
                    "state": "complete",
                    "candidates": [
                        {
                            "recording_id": "rec_ready_a",
                            "radio_id": "radio_a",
                            "lnb_id": "lnb-current-01",
                            "receiver_chain_id": "rx_current_01",
                            "segment_id": "seg_current_01",
                            "edge": "lower",
                            "qam_goodness": 0.81,
                            "hard_symbol_accuracy": 0.88,
                            "rms_evm": 0.65,
                            "window_count": 32,
                            "analysis_id": "slqam3rec_ready_a",
                            "selection": (
                                "highest-qam-goodness-per-recording-radio-lnb-receiver"
                            ),
                        }
                    ],
                    "reason_codes": [],
                }
            ],
            "original_recording_count": 1,
            "truncated": False,
            "warnings": [
                "candidate-only-qam-goodness-not-starlink-detection",
                "highest-goodness-selected-independently-per-authoritative-lnb-receiver",
                "best-analyzed-window-not-support-weighted-dwell-mean",
                "radio-lnb-receiver-series-are-never-pooled",
            ],
        }


class _SummaryPriorityProbe:
    def __init__(self) -> None:
        self.application: Any = None
        self.qam_finished = threading.Event()
        self.qam_request_count = 0
        self.duration_started_before_qam = False

    def handle(self, request: JsonRequest) -> JsonResponse:
        if request.path == "/api/v22/capture-qam-summaries":
            self.qam_request_count += 1
            time.sleep(0.12)
            response = self.application.handle(request)
            self.qam_finished.set()
            return response
        if request.path.startswith("/api/v3/recordings/"):
            self.duration_started_before_qam |= not self.qam_finished.is_set()
        return self.application.handle(request)


@contextmanager
def _running_master_dashboard(
    priority_probe: _SummaryPriorityProbe | None = None,
) -> Iterator[str]:
    queries = repository(50)
    ports = _MasterEvidencePorts()
    v3 = DashboardJsonApplicationV3(queries, queries, ports, ports, ports)
    v17 = DashboardJsonApplicationV17(cast(DashboardJsonApplicationV16, v3), ports)
    v18 = DashboardJsonApplicationV18(v17, ports)
    base_app = DashboardUiApplication(DashboardJsonApplicationV22(v18, ports))
    app: Any = base_app
    if priority_probe is not None:
        priority_probe.application = base_app
        app = priority_probe
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


def test_master_prioritizes_one_bounded_qam_summary_before_duration_enrichment() -> (
    None
):
    probe = _SummaryPriorityProbe()
    with _running_master_dashboard(probe) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=_browser_environment())
        try:
            page = browser.new_page()
            page.add_init_script(
                """Date.now = () => 7200000;"""
            )
            started = time.monotonic()
            response = page.goto(base_url)
            assert response is not None and response.ok
            row = page.locator('[data-attempt-id="cattempt_ready_a"]')
            expect(row.locator(".capture-qam-summary")).to_have_attribute(
                "data-state", "complete"
            )
            expect(row.locator(".capture-qam-candidate")).to_have_count(1)
            qam_elapsed = time.monotonic() - started
            expect(row.locator(".qam-detail-link")).to_have_attribute(
                "href", "/recordings/rec_ready_a#evidence-qam"
            )
            expect(row.locator(".capture-duration")).to_have_text(
                "60.00 s", timeout=15_000
            )
            assert probe.qam_finished.is_set()
            assert probe.qam_request_count == 1
            assert not probe.duration_started_before_qam
            assert qam_elapsed < 0.75
        finally:
            browser.close()
