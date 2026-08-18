from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.contracts.core import canonical_json_bytes
from leo_flow.dashboard.api import JsonRequest, JsonResponse
from leo_flow.dashboard.ui import DashboardUiApplication

RECORDING = "rec_facade_browser"


def _product(
    name: str, state: str, payload: object | None = None, source: str | None = None
) -> dict[str, object | None]:
    return {"product": name, "state": state, "source": source, "payload": payload}


def _primary_products() -> list[dict[str, object | None]]:
    context = {
        "requested_recording_id": RECORDING,
        "capture_batch_id": "batch_with_companion",
        "recordings": [
            {"recording_id": RECORDING, "radio_id": "radio_exact", "requested": True},
            {"recording_id": "rec_companion", "radio_id": "radio_leak", "requested": False},
        ],
        "receivers": [
            {
                "recording_id": RECORDING,
                "radio_id": "radio_exact",
                "receiver_chain_id": "rx_exact",
                "lnb_id": "lnb_exact",
            },
            {
                "recording_id": "rec_companion",
                "radio_id": "radio_leak",
                "receiver_chain_id": "rx_leak",
                "lnb_id": "lnb_leak",
            },
        ],
        "segments": [],
        "candidate_only": True,
        "calibrated_detection_count": None,
        "warnings": [],
        "limitations": [],
    }
    qam = {
        "recording_id": RECORDING,
        "candidate_only": True,
        "calibration_required": True,
        "source_adaptive_response_ref": {"artifact_id": "adaptive-response-1"},
        "streams": [
            {
                "recording_id": RECORDING,
                "radio_id": "radio_exact",
                "lnb_id": "lnb_exact",
                "receiver_chain_id": "rx_exact",
                "segment_id": "seg_exact",
                "edge": "lower",
                "sample_rate_hz": 1_000_000,
                "segment_sample_count": 100_000,
                "original_window_count": 1,
                "overall": {"derivation": "support-weighted-window-summary"},
                "windows": [
                    {
                        "selection": {
                            "qam_start_sample": 20_000,
                            "qam_stop_sample": 30_000,
                            "source_start_sample": 10_000,
                            "source_stop_sample": 20_000,
                            "reasons": ["qin-maximum"],
                            "source_qin_score": 0.9,
                            "source_max_surrogate_score": 0.2,
                            "source_qin_minus_max_surrogate": 0.7,
                        },
                        "qam": {
                            "window_index": 0,
                            "interval_start_utc_ns": 1_000_000_000,
                            "interval_stop_utc_ns": 1_010_000_000,
                            "winning_cfo_hz": 125_000.0,
                            "hard_symbol_accuracy": 0.94,
                            "rms_evm": 0.18,
                            "display_points": [
                                {"i": 0.7, "q": 0.7, "expected_state": 0},
                                {"i": -0.7, "q": 0.7, "expected_state": 1},
                            ],
                        },
                    }
                ],
            }
        ],
    }
    detector = {
        "recording_id": RECORDING,
        "candidate_only": True,
        "calibrated_detection_count": None,
        "plan": {"probe_sample_count": 10_000},
        "streams": [
            {
                "selection": {
                    "radio_id": "radio_exact",
                    "lnb_id": "lnb_exact",
                    "receiver_chain_id": "rx_exact",
                    "channel_number": 1,
                    "edge": "lower",
                    "sample_rate_hz": 1_000_000,
                    "exact_windows": [
                        {"start_sample": 10_000, "stop_sample": 20_000, "stage": "sentinel"}
                    ],
                },
                "exact_coverage_fraction": 0.1,
                "points": [
                    {
                        "method": "anchor-8",
                        "interval_start_utc_ns": 1_000_000_000,
                        "interval_stop_utc_ns": 1_010_000_000,
                        "qin": {"score": 0.91},
                        "surrogates": [{"winner": {"score": 0.22}}],
                    }
                ],
            }
        ],
    }
    return [
        _product("recording_facts", "complete", {"recording_id": RECORDING}, "detail"),
        _product("evidence_context", "complete", context, "context"),
        _product("qam", "complete", qam, "adaptive-qam-v0.4"),
        _product("adaptive_detector_response", "complete", detector, "adaptive-response-v0.1"),
        _product("doppler_summary", "not_analyzed"),
    ]


def _extended_products() -> list[dict[str, object | None]]:
    approach = {
        "recording_id": RECORDING,
        "candidate_only": True,
        "calibration_required": True,
        "qam_streams": [
            {
                "radio_id": "radio_exact",
                "lnb_id": "lnb_exact",
                "receiver_chain_id": "rx_exact",
                "edge": "lower",
                "window_count": 4,
                "window_sample_count": 10_000,
                "sample_rate_hz": 1_000_000,
                "sampling_plan": "dwell-stratified exact windows",
                "analyzed_union_sample_count": 40_000,
                "segment_sample_count": 100_000,
                "analyzed_union_fraction": 0.4,
                "searched_cfo_min_hz": -700_000,
                "searched_cfo_max_hz": 700_000,
                "coarse_search_cell_count": 1401,
                "refinement_search_cell_count": 81,
                "hardware_calibration_state": "uncalibrated",
                "receiver_cfo_profile_ids": ["profile_exact"],
                "winning_cfo_min_hz": 120_000,
                "winning_cfo_max_hz": 130_000,
                "overall_derivation": "support-weighted-window-summary",
                "retained_candidate_count": 2,
            }
        ],
    }
    basic_doppler = {
        "state": "complete",
        "candidate_only": True,
        "calibrated_detection_count": None,
        "series": [
            {
                "recording_id": RECORDING,
                "radio_id": "radio_exact",
                "lnb_id": "lnb_exact",
                "receiver_chain_id": "rx_exact",
                "segment_id": "seg_exact",
                "candidate_rank": 0,
                "total": {"drift_rate_hz_s": -315.5},
                "windows": [
                    {
                        "interval_start_utc_ns": 1_000_000_000,
                        "interval_stop_utc_ns": 1_010_000_000,
                        "drift_rate_hz_s": -315.5,
                    }
                ],
            }
        ],
    }
    values: dict[str, tuple[str, object | None, str | None]] = {
        "approaches": ("complete", approach, "analysis-approaches-v0.1"),
        "full_dwell_timeline": ("pending", None, None),
        "pilot_prescreen": ("no_candidate", None, None),
        "pilot_refinement": ("not_analyzed", None, None),
        "legacy_full_dwell": ("not_analyzed", None, None),
        "basic_doppler": ("complete", basic_doppler, "recording-evidence-doppler-v0.1"),
        "advanced_doppler": ("failed", None, None),
        "pilot_doppler_association": ("not_analyzed", None, None),
    }
    inventory = [
        "approaches", "full_dwell_timeline", "pilot_prescreen", "pilot_refinement",
        "legacy_full_dwell", "basic_doppler", "advanced_doppler",
        "pilot_doppler_association", "symbolwise_replay", "receiver_agnostic_cfo_qam",
        "legacy_suite", "waterfall", "doppler_visualization", "surrogate_null",
        "temporal_pilot", "pilot_constellation",
    ]
    return [
        _product(name, *(values.get(name) or ("not_analyzed", None, None)))
        for name in inventory
    ]


class _FacadeFixture:
    def handle(self, request: JsonRequest) -> JsonResponse:
        if request.path == f"/api/recordings/{RECORDING}/analysis":
            requested = request.query.get("sections", "primary").split(",")
            sections = []
            for section in requested:
                products = _primary_products() if section == "primary" else _extended_products()
                sections.append({"section": section, "products": products})
            payload = {
                "schema": "org.leo-flow.dashboard.recording-analysis-facade",
                "recording_id": RECORDING,
                "requested_sections": requested,
                "sections": sections,
            }
            return JsonResponse(200, (("content-type", "application/json"),), canonical_json_bytes(payload))
        return JsonResponse(404, (("content-type", "application/json"),), b'{"error":"absent"}')


class _FocusedRecordingUi:
    """Serve the real recording workspace with only this slice's owned client."""

    def __init__(self) -> None:
        self._ui = DashboardUiApplication(_FacadeFixture())

    def handle(self, request: JsonRequest) -> JsonResponse:
        response = self._ui.handle(request)
        if request.path == f"/recordings/{RECORDING}" and response.status == 200:
            body = response.body
            for asset in (b"recording-detail.js", b"symbolwise-replay.js", b"receiver-agnostic-cfo-qam.js"):
                lines = [line for line in body.splitlines() if asset not in line]
                body = b"\n".join(lines)
            return JsonResponse(response.status, response.headers, body)
        return response


@contextmanager
def running_dashboard() -> Iterator[str]:
    server = StdlibDashboardServer(request_timeout_s=0.05)
    server.preflight("127.0.0.1", 0)
    stopped = threading.Event()
    app = _FocusedRecordingUi()

    def serve() -> None:
        while not stopped.is_set():
            server.serve_once(app)

    worker = threading.Thread(target=serve, name="facade-browser-server")
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.bound_port}"
    finally:
        stopped.set()
        worker.join(timeout=1)
        server.close(1)
        assert not worker.is_alive()


def browser_environment() -> dict[str, str | float | bool]:
    environment: dict[str, str | float | bool] = dict(os.environ)
    root = Path.home() / ".cache" / "ms-playwright" / "ubuntu-libs"
    if root.is_dir():
        environment["LD_LIBRARY_PATH"] = str(root / "usr/lib/x86_64-linux-gnu")
        environment["FONTCONFIG_FILE"] = str(root / "etc/fonts/fonts.conf")
        environment["FONTCONFIG_SYSROOT"] = str(root)
    return environment


def test_recording_evidence_uses_only_the_unversioned_analysis_facade() -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            requests: list[str] = []
            bad_responses: list[tuple[str, int]] = []
            errors: list[str] = []
            page.on("request", lambda request: requests.append(request.url.removeprefix(base_url)))
            page.on("response", lambda response: bad_responses.append((response.url, response.status)) if response.status >= 400 else None)
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))

            response = page.goto(f"{base_url}/recordings/{RECORDING}")
            assert response is not None and response.ok
            expect(page.locator("#evidence-context-state")).to_have_attribute("data-state", "ready")
            expect(page.locator("#evidence-radios input")).to_have_count(1)
            expect(page.locator("#evidence-radios")).to_contain_text("radio_exact")
            expect(page.locator("#evidence-controls")).not_to_contain_text("radio_leak")
            expect(page.locator("#evidence-controls")).not_to_contain_text("lnb_leak")
            expect(page.locator("#evidence-controls")).not_to_contain_text("rx_leak")
            expect(page.locator("#evidence-qam-state")).to_have_attribute("data-product-state", "complete")
            expect(page.locator("#evidence-qam-state")).to_have_attribute("data-state", "ready")
            expect(page.locator("#evidence-detector-state")).to_have_attribute("data-state", "ready")

            page.locator("#evidence-load-extended").click()
            expect(page.locator("#evidence-load-extended")).to_have_text("Extended analysis loaded")
            expect(page.locator("#evidence-timeline-state")).to_have_attribute("data-product-state", "pending")
            expect(page.locator("#evidence-prescreen-state")).to_have_attribute("data-product-state", "no_candidate")
            expect(page.locator("#evidence-doppler-state")).to_have_attribute("data-product-state", "failed")
            expect(page.locator("#evidence-pilot-doppler-state")).to_have_attribute("data-product-state", "not_analyzed")
            expect(page.locator("#evidence-doppler-state")).to_have_attribute("data-state", "ready")
            expect(page.locator('#evidence-approaches-body tr[data-approach="qam"]')).to_contain_text("dwell-stratified exact windows")
            expect(page.locator("#evidence-approaches-body")).to_contain_text("−700.0…700.0 kHz".replace("−", "-"))
            expect(page.locator("#evidence-qam-goodness")).to_contain_text("94.00%")
            expect(page.locator("#evidence-detector-legend")).to_contain_text("anchor-8")
            expect(page.locator("#evidence-doppler-legend")).to_contain_text("basic candidate 0")

            assert not any(path.startswith("/api/v") for path in requests), requests
            assert bad_responses == []
            assert errors == []
            analysis = [path for path in requests if path.startswith(f"/api/recordings/{RECORDING}/analysis?")]
            assert any("sections=primary" in path for path in analysis)
            assert any("sections=extended" in path for path in analysis)
        finally:
            browser.close()
