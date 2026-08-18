from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from playwright.sync_api import expect, sync_playwright

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.contracts.core import canonical_json_bytes
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV28,
    DashboardJsonApplicationV29,
    DashboardPublicJsonApplication,
    JsonRequest,
    JsonResponse,
)
from leo_flow.dashboard.ui import DashboardUiApplication

RECORDING = "rec_symbolwise_browser"


def _context() -> dict[str, Any]:
    return {
        "requested_recording_id": RECORDING,
        "capture_batch_id": None,
        "recordings": [
            {
                "recording_id": RECORDING,
                "radio_id": "radio_a",
                "radio_serial": "serial-a",
                "hardware_snapshot_id": "hw_a",
                "capture_started_utc_ns": 1_800_000_000_000_000_000,
                "capture_finished_utc_ns": 1_800_000_060_000_000_000,
                "analysis_state": "complete",
                "requested": True,
            }
        ],
        "receivers": [
            {
                "recording_id": RECORDING,
                "radio_id": "radio_a",
                "receiver_chain_id": "rx_1",
                "radio_channel": 1,
                "lnb_id": "lnb-a",
                "polarization": "H",
                "valid_from_utc_ns": 1,
                "valid_until_utc_ns": None,
            },
            {
                "recording_id": RECORDING,
                "radio_id": "radio_a",
                "receiver_chain_id": "rx_2",
                "radio_channel": 2,
                "lnb_id": "lnb-b",
                "polarization": "V",
                "valid_from_utc_ns": 1,
                "valid_until_utc_ns": None,
            },
        ],
        "segments": [
            {
                "recording_id": RECORDING,
                "segment_id": "seg_a",
                "receiver_chain_ids": ["rx_1", "rx_2"],
            }
        ],
        "candidate_only": True,
        "calibrated_detection_count": None,
        "warnings": ["candidate-only-evidence-not-calibrated-detection"],
        "limitations": [],
    }


def _stream(receiver: str, lnb: str, offset: float) -> dict[str, Any]:
    patterns = [
        ("qin", "qin-exact", None),
        *[(f"surrogate-{i}", "precommitted-surrogate", i) for i in range(4)],
    ]
    windows = []
    for window in range(600):
        windows.append(
            {
                "window_index": window,
                "start_sample": window * 250_000,
                "stop_sample": window * 250_000 + 25_000,
                "start_time_s": window / 10,
                "stop_time_s": window / 10 + 0.01,
                "patterns": [
                    {
                        "pattern_id": pattern_id,
                        "pattern_role": role,
                        "codebook_index": codebook,
                        "candidate_label": "Candidate-only · Qin exact"
                        if codebook is None
                        else f"Candidate-only · surrogate {codebook + 1}",
                        "selection_score": min(
                            0.99, 0.1 + offset + pattern_index * 0.05 + window / 10_000
                        ),
                        "winning_cfo_hz": 12_345.0 + window + pattern_index,
                        "winning_epoch_sample": 70 + pattern_index,
                    }
                    for pattern_index, (pattern_id, role, codebook) in enumerate(
                        patterns
                    )
                ],
            }
        )
    overall = []
    for pattern_index, (pattern_id, role, codebook) in enumerate(patterns):
        overall.append(
            {
                "pattern_id": pattern_id,
                "pattern_role": role,
                "codebook_index": codebook,
                "candidate_label": "Candidate-only · Qin exact"
                if codebook is None
                else f"Candidate-only · surrogate {codebook + 1}",
                "mean_selection_score": 0.2 + offset + pattern_index * 0.05,
                "maximum_selection_score": 0.8 + offset,
                "winning_window_index": 599,
                "winning_window_start_time_s": 59.9,
                "winning_cfo_hz": 12_944.0 + pattern_index,
                "winning_epoch_sample": 70 + pattern_index,
                "derivation": "arithmetic-mean-and-maximum-selection-score-over-all-600-fixed-cadence-windows;ties-first-window",
            }
        )
    return {
        "recording_id": RECORDING,
        "radio_id": "radio_a",
        "lnb_id": lnb,
        "receiver_chain_id": receiver,
        "segment_id": "seg_a",
        "edge": "lower",
        "sample_rate_hz": 2_500_000.0,
        "frequency_center_cfo_hz": 25_000.0,
        "window_count": 600,
        "window_duration_ms": 10,
        "cadence_ms": 100,
        "analyzed_union_fraction": 0.1,
        "analyzed_union_percent": 10.0,
        "windows": windows,
        "overall": overall,
        "candidates_only": True,
    }


class _Previous:
    def handle(self, request: JsonRequest) -> JsonResponse:
        if request.path == "/api/recordings":
            payload = {
                "items": [
                    {
                        "recording_id": RECORDING,
                        "radio_id": "radio_a",
                        "started_utc_ns": 1_800_000_000_000_000_000,
                        "activity_kinds": ["dwell"],
                        "analysis_state": "complete",
                    }
                ],
                "next_cursor": None,
            }
            return JsonResponse(
                200,
                (("content-type", "application/json"),),
                canonical_json_bytes(payload),
            )
        if request.path == f"/api/v16/recordings/{RECORDING}/evidence-context":
            return JsonResponse(
                200,
                (("content-type", "application/json"),),
                canonical_json_bytes(_context()),
            )
        return JsonResponse(
            404,
            (("content-type", "application/json"),),
            b'{"error":{"code":"not_found","message":"fixture route absent"}}',
        )


class _Symbolwise:
    def __init__(self) -> None:
        self.state = "populated"
        self.queries: list[Any] = []

    def recording_symbolwise_replay_dashboard(self, query):  # type: ignore[no-untyped-def]
        self.queries.append(query)
        if self.state == "pending":
            raise LookupError("not published")
        if self.state == "error":
            raise RuntimeError("projection unavailable")
        streams = [
            _stream("rx_1", "lnb-a", 0.0),
            _stream("rx_2", "lnb-b", 0.02),
        ]
        if query.lnb_ids:
            streams = [item for item in streams if item["lnb_id"] in query.lnb_ids]
        if query.receiver_chain_ids:
            streams = [
                item
                for item in streams
                if item["receiver_chain_id"] in query.receiver_chain_ids
            ]
        return {
            "recording_id": RECORDING,
            "streams": streams,
            "stream_count": len(streams),
            "window_count_per_stream": 600,
            "point_count": len(streams) * 600,
            "candidate_only": True,
            "calibrated_detection_count": None,
            "summary_derivation": "per-stream-per-pattern-only;arithmetic-mean-and-maximum-over-all-600-windows;no-cross-hardware-pooling",
            "limitations": ["finite-pattern-controls-not-empirical-null"],
        }


@contextmanager
def _running(port: _Symbolwise) -> Iterator[str]:
    server = StdlibDashboardServer(
        request_timeout_s=0.01, maximum_concurrent_requests=4
    )
    server.preflight("127.0.0.1", 0)
    api = DashboardJsonApplicationV29(
        cast(DashboardJsonApplicationV28, _Previous()), port
    )
    application = DashboardUiApplication(DashboardPublicJsonApplication(api))
    stopped = threading.Event()

    def serve() -> None:
        while not stopped.is_set():
            server.serve_once(application)

    worker = threading.Thread(target=serve, name="symbolwise-dashboard-browser")
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


def test_real_v29_handler_renders_complete_filtered_curves_and_truthful_states() -> (
    None
):
    port = _Symbolwise()
    with _running(port) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=_browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            page.goto(base_url)
            master_link = page.get_by_role(
                "link", name=f"Open full capture page for {RECORDING}"
            )
            expect(master_link).to_have_attribute("href", f"/recordings/{RECORDING}")
            master_link.click()
            expect(page).to_have_url(f"{base_url}/recordings/{RECORDING}")
            expect(page.locator("#evidence-symbolwise-state")).to_have_attribute(
                "data-state", "pending"
            )
            page.locator("#evidence-load-extended").click()
            expect(page.locator("#evidence-symbolwise-state")).to_have_attribute(
                "data-state", "ready", timeout=15_000
            )
            expect(page.locator("#evidence-symbolwise-facts")).to_contain_text(
                "600 exact 10 ms windows"
            )
            expect(page.locator("#evidence-symbolwise-facts")).to_contain_text(
                "6.000 s / 60.000 s = 10% exactly"
            )
            expect(page.locator("#evidence-symbolwise-canvas")).to_be_visible()
            expect(page.locator("#evidence-symbolwise-window-body")).to_contain_text(
                "12345.000 Hz"
            )
            expect(page.locator("#evidence-symbolwise-window-body")).to_contain_text(
                "70 samples"
            )
            expect(page.locator("#evidence-symbolwise-overall-body")).to_contain_text(
                "Candidate-only · Qin exact"
            )
            expect(page.locator("#evidence-symbolwise-overall-body")).to_contain_text(
                "maximum-selection-score-over-all-600"
            )

            page.locator('#evidence-patterns input[value="surrogate-1"]').check()
            expect(page.locator("#evidence-symbolwise-window-body")).to_contain_text(
                "Candidate-only · surrogate 2"
            )
            page.locator('#evidence-lnbs input[value="lnb-b"]').uncheck()
            expect(page.locator("#evidence-symbolwise-state")).to_contain_text(
                "600 complete fixed-cadence window points", timeout=15_000
            )
            assert port.queries[-1].lnb_ids == ("lnb-a",)
            assert port.queries[-1].receiver_chain_ids == ("rx_1", "rx_2")

            port.state = "pending"
            page.locator('#evidence-patterns input[value="surrogate-2"]').check()
            expect(page.locator("#evidence-symbolwise-state")).to_have_attribute(
                "data-state", "pending"
            )
            expect(page.locator("#evidence-symbolwise-state")).to_contain_text(
                "pending or has not been published"
            )
            port.state = "error"
            page.locator('#evidence-patterns input[value="surrogate-3"]').check()
            expect(page.locator("#evidence-symbolwise-state")).to_have_attribute(
                "data-state", "error"
            )
            expect(page.locator("#evidence-symbolwise-state")).to_contain_text(
                "dashboard query failed"
            )
        finally:
            browser.close()
