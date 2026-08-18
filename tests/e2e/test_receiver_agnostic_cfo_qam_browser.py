from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from playwright.sync_api import expect, sync_playwright

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.contracts.core import canonical_json_bytes
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV29,
    DashboardJsonApplicationV30,
    DashboardPublicJsonApplication,
    JsonRequest,
    JsonResponse,
)
from leo_flow.dashboard.ui import DashboardUiApplication

RECORDING = "rec_cfo_qam_browser"


class _Previous:
    def handle(self, request: JsonRequest) -> JsonResponse:
        if request.path == f"/api/v16/recordings/{RECORDING}/evidence-context":
            payload = {
                "requested_recording_id": RECORDING,
                "capture_batch_id": None,
                "recordings": [
                    {
                        "recording_id": RECORDING,
                        "radio_id": "radio_a",
                        "requested": True,
                    },
                    {
                        "recording_id": "rec_companion",
                        "radio_id": "radio_b",
                        "requested": False,
                    },
                ],
                "receivers": [
                    {
                        "recording_id": RECORDING,
                        "radio_id": "radio_a",
                        "receiver_chain_id": "rx_a",
                        "lnb_id": "physical-a",
                    },
                    {
                        "recording_id": RECORDING,
                        "radio_id": "radio_a",
                        "receiver_chain_id": "rx_b",
                        "lnb_id": "physical-b",
                    },
                ],
                "segments": [],
                "candidate_only": True,
                "calibrated_detection_count": None,
                "limitations": [],
            }
            return JsonResponse(
                200,
                (("content-type", "application/json"),),
                canonical_json_bytes(payload),
            )
        return JsonResponse(
            404,
            (("content-type", "application/json"),),
            b'{"error":{"code":"not_found","message":"fixture route absent"}}',
        )


class _Port:
    def __init__(self) -> None:
        self.queries = []

    def recording_receiver_agnostic_cfo_qam(self, query):  # type: ignore[no-untyped-def]
        self.queries.append(query)
        windows = [self._window("rx_a", 350000.0), self._window("rx_b", -350000.0)]
        windows = [
            item
            for item in windows
            if (not query.radio_ids or item["radio_id"] in query.radio_ids)
            and (
                not query.receiver_chain_ids
                or item["receiver_chain_id"] in query.receiver_chain_ids
            )
        ]
        return {
            "recording_id": str(query.recording_id),
            "returned_window_count": len(windows),
            "total_window_count": len(windows),
            "truncated": False,
            "candidates_only": True,
            "calibrated_detection_count": None,
            "limitations": [],
            "windows": windows,
        }

    @staticmethod
    def _window(receiver: str, cfo_hz: float):  # type: ignore[no-untyped-def]
        return {
            "radio_id": "radio_a",
            "receiver_chain_id": receiver,
            "edge": "lower",
            "start_sample": 0,
            "stop_sample": 7500,
            "sample_rate_hz": 2500000.0,
            "cfo_min_hz": -700000.0,
            "cfo_max_hz": 700000.0,
            "coarse_cell_count": 5,
            "local_cell_count": 1,
            "unique_cell_count": 6,
            "pattern_evaluation_count": 12,
            "patterns": [
                {
                    "pattern_index": 0,
                    "winning_epoch_sample": 63,
                    "winning_cfo_hz": cfo_hz,
                    "winning_score": 0.75,
                    "hard_symbol_accuracy": 0.9,
                    "rms_evm": 0.1,
                }
            ],
        }


@contextmanager
def _running(port: _Port) -> Iterator[str]:
    server = StdlibDashboardServer(
        request_timeout_s=0.01, maximum_concurrent_requests=4
    )
    server.preflight("127.0.0.1", 0)
    api = DashboardJsonApplicationV30(
        cast(DashboardJsonApplicationV29, _Previous()), port
    )
    application = DashboardUiApplication(DashboardPublicJsonApplication(api))
    stopped = threading.Event()
    worker = threading.Thread(
        target=lambda: _serve(server, application, stopped), name="cfo-qam-browser"
    )
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.bound_port}"
    finally:
        stopped.set()
        worker.join(timeout=1)
        server.close(1)
        assert not worker.is_alive()


def _serve(server, application, stopped):  # type: ignore[no-untyped-def]
    while not stopped.is_set():
        server.serve_once(application)


def _environment() -> dict[str, str | float | bool]:
    environment: dict[str, str | float | bool] = dict(os.environ)
    root = Path.home() / ".cache" / "ms-playwright" / "ubuntu-libs"
    if root.is_dir():
        environment["LD_LIBRARY_PATH"] = str(root / "usr/lib/x86_64-linux-gnu")
        environment["FONTCONFIG_FILE"] = str(root / "etc/fonts/fonts.conf")
        environment["FONTCONFIG_SYSROOT"] = str(root)
    return environment


def test_v06_diagnostic_renders_declared_domain_without_hardware_center_labels() -> (
    None
):
    port = _Port()
    with _running(port) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=_environment())
        try:
            page = browser.new_page()
            page.goto(f"{base_url}/recordings/{RECORDING}")
            expect(page.locator("#evidence-context-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(
                page.locator("#evidence-receiver-agnostic-cfo-state")
            ).to_have_attribute("data-state", "pending")
            assert port.queries == []
            page.locator("#evidence-load-extended").click()
            expect(
                page.locator("#evidence-receiver-agnostic-cfo-state")
            ).to_have_attribute("data-state", "ready")
            assert port.queries[-1].radio_ids == ("radio_a",)
            assert port.queries[-1].receiver_chain_ids == ("rx_a", "rx_b")
            expect(
                page.locator("#evidence-receiver-agnostic-cfo-facts")
            ).to_contain_text("-700 to 700 kHz")
            expect(
                page.locator("#evidence-receiver-agnostic-cfo-body")
            ).to_contain_text("350000 Hz / 63")
            card = page.locator("#evidence-receiver-agnostic-cfo")
            expect(card).to_contain_text(
                "not the complete current wide production search"
            )
            expect(card).not_to_contain_text("LNB center")
            page.locator('#evidence-receivers input[value="rx_b"]').uncheck()
            expect(
                page.locator("#evidence-receiver-agnostic-cfo-body")
            ).not_to_contain_text("rx_b")
            assert port.queries[-1].radio_ids == ("radio_a",)
            assert port.queries[-1].receiver_chain_ids == ("rx_a",)
        finally:
            browser.close()
