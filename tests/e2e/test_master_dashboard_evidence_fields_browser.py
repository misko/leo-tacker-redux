from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from playwright.sync_api import expect, sync_playwright

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.contracts.core import (
    CaptureAttemptId,
    CaptureBatchId,
    RadioId,
    RecordingId,
    UtcNs,
)
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV3,
    DashboardJsonApplicationV16,
    DashboardJsonApplicationV17,
    DashboardJsonApplicationV18,
    DashboardJsonApplicationV22,
    JsonRequest,
    JsonResponse,
)
from leo_flow.dashboard.repository import (
    CaptureBatchProjection,
    InMemoryDashboardRepository,
)
from leo_flow.dashboard.ui import DashboardUiApplication
from tests.dashboard._fixtures import capture_batches, repository


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
        assert query.maximum_recordings == 100
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
                },
                {
                    "recording_id": "rec_ready_b",
                    "radio_id": "radio_b",
                    "analysis_state": "complete",
                    "state": "unavailable",
                    "candidates": [],
                    "reason_codes": ["published-acquired-qam-summary-unavailable"],
                },
                {
                    "recording_id": "rec_pending_a",
                    "radio_id": "radio_a",
                    "analysis_state": "pending",
                    "state": "pending",
                    "candidates": [],
                    "reason_codes": ["acquired-qam-analysis-pending"],
                },
                *(
                    {
                        "recording_id": recording_id,
                        "radio_id": radio_id,
                        "analysis_state": "complete",
                        "state": "unavailable",
                        "candidates": [],
                        "reason_codes": ["published-acquired-qam-summary-unavailable"],
                    }
                    for recording_id, radio_id in (
                        ("rec_solo_preserved", "radio_a"),
                        ("rec_skew_a", "radio_a"),
                        ("rec_skew_b", "radio_b"),
                    )
                ),
            ],
            "original_recording_count": 6,
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
        self.qam_detail_request_count = 0
        self.duration_started_before_qam = False

    def handle(self, request: JsonRequest) -> JsonResponse:
        if request.path == "/api/v22/capture-qam-summaries":
            self.qam_request_count += 1
            time.sleep(0.12)
            response = self.application.handle(request)
            self.qam_finished.set()
            return response
        if (
            "starlink-acquired-constellation" in request.path
            or "adaptive-qam" in request.path
        ):
            self.qam_detail_request_count += 1
        if request.path.startswith("/api/v3/recordings/"):
            self.duration_started_before_qam |= not self.qam_finished.is_set()
        return self.application.handle(request)


@contextmanager
def _running_master_dashboard(
    priority_probe: _SummaryPriorityProbe | None = None,
    *,
    capture_queries: InMemoryDashboardRepository | None = None,
    ports: _MasterEvidencePorts | None = None,
) -> Iterator[str]:
    queries = repository(50)
    ports = ports or _MasterEvidencePorts()
    v3 = DashboardJsonApplicationV3(
        queries, capture_queries or queries, ports, ports, ports
    )
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
            for attempt_id, recording_id, state in (
                ("cattempt_ready_b", "rec_ready_b", "unavailable"),
                ("cattempt_pending_a", "rec_pending_a", "pending"),
                ("cattempt_peer_failed_a", "rec_solo_preserved", "unavailable"),
                ("cattempt_skew_a", "rec_skew_a", "unavailable"),
                ("cattempt_skew_b", "rec_skew_b", "unavailable"),
            ):
                other_qam = page.locator(
                    f'[data-attempt-id="{attempt_id}"] .capture-qam-summary'
                )
                expect(other_qam).to_have_attribute("data-state", state)
                expect(other_qam.locator(".qam-detail-link")).to_have_attribute(
                    "href", f"/recordings/{recording_id}#evidence-qam"
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


class _HundredQamPorts(_MasterEvidencePorts):
    def capture_qam_summaries(self, query: Any) -> dict[str, object]:
        assert query.maximum_recordings == 100
        return {
            "schema_version": 1,
            "start_utc_ns": query.start_utc_ns,
            "stop_utc_ns": query.stop_utc_ns,
            "candidate_only": True,
            "calibration_required": True,
            "calibrated_detection_count": None,
            "recordings": [
                {
                    "recording_id": f"rec_v22_{index:03d}",
                    "radio_id": f"radio_j1_{'a' if index % 2 == 0 else 'b'}",
                    "analysis_state": "complete",
                    "state": "complete",
                    "candidates": [
                        {
                            "recording_id": f"rec_v22_{index:03d}",
                            "radio_id": (
                                f"radio_j1_{'a' if index % 2 == 0 else 'b'}"
                            ),
                            "lnb_id": "lnb-j1",
                            "receiver_chain_id": "rx-j1",
                            "segment_id": f"seg_v22_{index:03d}",
                            "edge": "lower",
                            "qam_goodness": 0.91,
                            "hard_symbol_accuracy": 0.94,
                            "rms_evm": 0.42,
                            "window_count": 32,
                            "analysis_id": f"slqam3_v22_{index:03d}",
                            "selection": (
                                "highest-qam-goodness-per-recording-radio-lnb-receiver"
                            ),
                        }
                    ],
                    "reason_codes": [],
                }
                for index in range(100)
            ],
            "original_recording_count": 100,
            "truncated": False,
            "warnings": [
                "candidate-only-qam-goodness-not-starlink-detection",
                "highest-goodness-selected-independently-per-authoritative-lnb-receiver",
                "best-analyzed-window-not-support-weighted-dwell-mean",
                "radio-lnb-receiver-series-are-never-pooled",
            ],
        }

    def capture_doppler_summaries(self, query: Any) -> dict[str, object]:
        del query
        return {
            "candidate_only": True,
            "calibrated_detection_count": None,
            "warnings": ["radio-lnb-receiver-candidates-are-never-pooled"],
            "recordings": [],
        }


def _hundred_capture_repository() -> InMemoryDashboardRepository:
    template = capture_batches()[0].view
    projections = []
    for batch_index in range(50):
        attempts = tuple(
            replace(
                template_attempt,
                attempt_id=CaptureAttemptId(f"cattempt_v22_{index:03d}"),
                radio_id=RadioId(f"radio_j1_{'a' if index % 2 == 0 else 'b'}"),
                requested_start_utc_ns=UtcNs(1_000_000_000 + index),
                observed_start_utc_ns=UtcNs(1_000_000_000 + index),
                recording_id=RecordingId(f"rec_v22_{index:03d}"),
            )
            for template_attempt, index in zip(
                template.attempts,
                (batch_index * 2, batch_index * 2 + 1),
                strict=True,
            )
        )
        projections.append(
            CaptureBatchProjection(
                replace(
                    template,
                    batch_id=CaptureBatchId(f"cbatch_v22_{batch_index:03d}"),
                    attempts=attempts,
                ),
                batch_index + 1,
            )
        )
    return InMemoryDashboardRepository(capture_batches=projections, page_size=100)


def test_master_v22_populates_the_full_bounded_one_hundred_recording_page() -> None:
    with _running_master_dashboard(
        capture_queries=_hundred_capture_repository(), ports=_HundredQamPorts()
    ) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=_browser_environment())
        try:
            page = browser.new_page()
            page.add_init_script("""Date.now = () => 7200000;""")
            response = page.goto(base_url)
            assert response is not None and response.ok

            rows = page.locator("#capture-attempts-body tr")
            expect(rows).to_have_count(100)
            qam_cells = rows.locator(".capture-qam-summary")
            expect(qam_cells).to_have_count(100)
            expect(qam_cells).to_have_attribute("data-state", "complete")
            expect(qam_cells.locator(".capture-qam-candidate")).to_have_count(100)
            links = qam_cells.locator(".qam-detail-link")
            expect(links).to_have_count(100)
            for index in (0, 50, 99):
                recording_id = f"rec_v22_{index:03d}"
                row = page.locator(f'[data-attempt-id="cattempt_v22_{index:03d}"]')
                expect(row.locator("th, td")).to_have_count(9)
                expect(row).to_have_attribute(
                    "aria-label",
                    f"View capture details, waterfall, and analysis for {recording_id}",
                )
                expect(row.locator(".qam-detail-link")).to_have_attribute(
                    "href", f"/recordings/{recording_id}#evidence-qam"
                )
                expect(row.locator(".capture-doppler-summary")).to_have_attribute(
                    "data-state", "unavailable"
                )
                expect(row.locator(".pilot-detection-counts")).to_have_text("— / —")
                expect(row.locator("td").nth(7)).to_have_text("—")
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
            page.add_init_script("""Date.now = () => 7200000;""")
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
            assert probe.qam_detail_request_count == 0
            assert not probe.duration_started_before_qam
            assert qam_elapsed < 0.75
        finally:
            browser.close()
