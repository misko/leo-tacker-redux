from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, expect, sync_playwright

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.dashboard.api import DashboardJsonApplicationV2, JsonRequest, JsonResponse
from leo_flow.dashboard.ui import DashboardUiApplication
from tests.dashboard._fixtures import repository

_NOW_MS = 7_200_000
_START_NS = 0
_STOP_NS = 7_200_000_000_000
_QAM_STATES = ("complete", "pending", "no_candidate", "not_analyzed", "failed")


class _SnapshotApi:
    def __init__(self) -> None:
        self.requests: list[JsonRequest] = []
        self._previous = DashboardJsonApplicationV2(repository(), repository())

    def handle(self, request: JsonRequest) -> JsonResponse:
        self.requests.append(request)
        if request.method == "GET" and request.path == "/api/captures":
            cursor = request.query.get("cursor")
            payload = _snapshot(cursor=cursor)
            return JsonResponse(
                200,
                (("content-type", "application/json"),),
                json.dumps(payload, separators=(",", ":")).encode(),
            )
        return self._previous.handle(request)


@contextmanager
def _running_dashboard(api: _SnapshotApi) -> Iterator[str]:
    server = StdlibDashboardServer(request_timeout_s=0.01)
    server.preflight("127.0.0.1", 0)
    application = DashboardUiApplication(api)
    stopped = threading.Event()

    def serve() -> None:
        while not stopped.is_set():
            server.serve_once(application)

    worker = threading.Thread(target=serve, name="master-capture-browser-server")
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.bound_port}"
    finally:
        stopped.set()
        worker.join(timeout=1)
        server.close(1)
        assert not worker.is_alive()


def _browser_environment() -> dict[str, str | float | bool]:
    import os

    environment: dict[str, str | float | bool] = dict(os.environ)
    root = Path.home() / ".cache" / "ms-playwright" / "ubuntu-libs"
    if root.is_dir():
        environment["LD_LIBRARY_PATH"] = str(root / "usr/lib/x86_64-linux-gnu")
        environment["FONTCONFIG_FILE"] = str(root / "etc/fonts/fonts.conf")
        environment["FONTCONFIG_SYSROOT"] = str(root)
    return environment


def _freeze_clock(page: Page) -> None:
    page.add_init_script(
        f"""
        (() => {{
          const fixedNow = {_NOW_MS};
          const NativeDate = Date;
          class FixedDate extends NativeDate {{
            constructor(...args) {{ super(...(args.length ? args : [fixedNow])); }}
            static now() {{ return fixedNow; }}
          }}
          FixedDate.parse = NativeDate.parse;
          FixedDate.UTC = NativeDate.UTC;
          globalThis.Date = FixedDate;
        }})();
        """
    )


def _qam(recording_id: str, radio_id: str, state: str) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    if state == "complete":
        candidates = [
            {
                "recording_id": recording_id,
                "radio_id": radio_id,
                "lnb_id": "lnb_a",
                "receiver_chain_id": "rx_a",
                "segment_id": "seg_a",
                "edge": "lower",
                "qam_goodness": 0.875,
                "hard_symbol_accuracy": 0.883,
                "rms_evm": 0.638,
                "window_count": 24,
                "analysis_id": f"qam_{recording_id}_a",
            },
            {
                "recording_id": recording_id,
                "radio_id": radio_id,
                "lnb_id": "lnb_b",
                "receiver_chain_id": "rx_b",
                "segment_id": "seg_b",
                "edge": "upper",
                "qam_goodness": 0.512,
                "hard_symbol_accuracy": 0.721,
                "rms_evm": 0.944,
                "window_count": 24,
                "analysis_id": f"qam_{recording_id}_b",
            },
        ]
    return {
        "state": state,
        "candidates": candidates,
        "reason_codes": [] if state == "complete" else [f"qam-{state}"],
    }


def _attempt(index: int) -> dict[str, Any]:
    state = _QAM_STATES[index % len(_QAM_STATES)]
    recording_id = f"rec_{index:03d}"
    radio_id = "radio_a" if index % 2 == 0 else "radio_b"
    return {
        "attempt_id": f"cattempt_{index:03d}",
        "radio_id": radio_id,
        "plan_id": f"plan_{index:03d}",
        "requested_start_utc_ns": index + 1,
        "capture_state": "succeeded",
        "observed_start_utc_ns": index + 1,
        "recording_id": recording_id,
        "failure_reason": None,
        "analysis_state": "pending" if state == "pending" else "complete",
        "analysis_result_available": state != "pending",
        "detail_href": f"/recordings/{recording_id}",
        "capture_duration_ns": 60_000_000_000,
        "qam": _qam(recording_id, radio_id, state),
        "doppler": {
            "state": "complete",
            "candidates": [
                {
                    "recording_id": recording_id,
                    "radio_id": radio_id,
                    "lnb_id": "lnb_a",
                    "receiver_chain_id": "rx_a",
                    "segment_id": "seg_a",
                    "candidate_id": f"doppler_candidate_{index:03d}",
                    "model": "linear",
                    "drift_rate_hz_s": 123.0,
                    "ranking_score": 0.9,
                    "doppler_id": f"doppler_{index:03d}",
                    "algorithm_version": "v1",
                }
            ],
            "reason_codes": [],
        },
        "pilot": {
            "state": "complete",
            "anchor_8_detection_count": 2,
            "glrt_detection_count": 1,
            "reason_codes": [],
        },
        "satellites": {
            "state": "complete",
            "count": 3,
            "reason_codes": [],
        },
    }


def _batch(index: int, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "batch_id": f"cbatch_{index:03d}",
        "mode": "independent",
        "coordination_claim": "none",
        "attempts": attempts,
        "revision": 1,
        "requested_start_skew_ns": 0,
        "observed_start_skew_ns": None,
        "maximum_observed_start_skew_ns": None,
        "paired_analysis_eligibility": "pending",
    }


def _snapshot(*, cursor: str | None) -> dict[str, Any]:
    first_page = cursor is None
    attempts = [_attempt(index) for index in range(100)] if first_page else [_attempt(100)]
    if not first_page:
        attempts.append(_attempt(101))
    batches = [
        _batch((0 if first_page else 50) + index // 2, attempts[index : index + 2])
        for index in range(0, len(attempts), 2)
    ]
    return {
        "schema_version": 1,
        "start_utc_ns": _START_NS,
        "stop_utc_ns": _STOP_NS,
        "items": batches,
        "next_cursor": "page-2" if first_page else None,
        "observation_aggregate": {
            "state": "complete",
            "reason_codes": [],
            "value": {
                "recording_count": 100,
                "candidate_recording_count": 4,
                "not_evaluated_recording_count": 3,
                "unavailable_recording_count": 2,
                "duty_cycles": [],
                "starlink_evidence": [],
                "recording_states": [],
            },
        },
        "retro_qam_canary": {
            "state": "complete",
            "reason_codes": [],
            "value": {
                "candidate_only": True,
                "calibrated_detection": None,
                "metrics_match_oracle": True,
                "combined_qam_goodness": 0.875449,
                "combined_hard_symbol_accuracy": 0.883333,
                "combined_rms_evm": 0.638171,
                "completed_utc_ns": 7_100_000_000_000,
                "schedule_interval_seconds": 1800,
                "corpus_id": "retro-qam-2026-08-17-v1",
                "iq_object_digest": {"value": "1" * 64},
                "receipt_digest": {"value": "2" * 64},
                "git_commit": "a4d9d183e0b11ccfafd010d2e70a2489cc65499f",
                "receivers": [],
            },
        },
        "warnings": [
            "candidate-only-qam-goodness-not-starlink-detection",
            "radio-lnb-receiver-series-are-never-pooled",
            "highest-goodness-selected-independently-per-authoritative-lnb-receiver",
        ],
    }


def test_initial_master_capture_snapshot_has_no_iterative_enrichment_or_loading() -> None:
    api = _SnapshotApi()
    with _running_dashboard(api) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=_browser_environment())
        try:
            page = browser.new_page()
            _freeze_clock(page)
            requested: list[str] = []
            failed: list[str] = []
            bad_responses: list[tuple[int, str]] = []
            console_errors: list[str] = []
            page_errors: list[str] = []
            page.on("request", lambda request: requested.append(request.url))
            page.on("requestfailed", lambda request: failed.append(request.url))
            page.on(
                "response",
                lambda response: bad_responses.append((response.status, response.url))
                if response.status >= 400
                else None,
            )
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )
            page.on("pageerror", lambda error: page_errors.append(str(error)))

            response = page.goto(base_url)
            assert response is not None and response.ok
            expect(page.locator("#capture-batches-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#observation-aggregate-state")).to_have_attribute(
                "data-state", "warning"
            )
            expect(page.locator("#observation-metrics")).to_contain_text("100")
            expect(page.locator("#retro-qam-canary-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#retro-qam-canary-metrics")).to_contain_text(
                "0.875"
            )
            rows = page.locator("#capture-attempts-body tr")
            expect(rows).to_have_count(100)
            assert rows.filter(has_text="Loading").count() == 0

            qam_cells = rows.locator(".capture-qam-summary")
            expect(qam_cells).to_have_count(100)
            assert qam_cells.filter(has_text="Loading").count() == 0
            for state, expected in (
                ("complete", "0.875"),
                ("pending", "Pending"),
                ("no_candidate", "No candidate"),
                ("not_analyzed", "Not analyzed"),
                ("failed", "Failed"),
            ):
                cell = page.locator(f'.capture-qam-summary[data-state="{state}"]').first
                expect(cell).to_contain_text(expected)
            expect(
                page.locator('[data-recording-id="rec_000"] .qam-detail-link')
            ).to_have_attribute("href", "/recordings/rec_000#evidence-qam")
            expect(page.locator('[data-recording-id="rec_000"]')).to_have_attribute(
                "aria-label", "View capture details, waterfall, and analysis for rec_000"
            )
            expect(
                page.locator('[data-recording-id="rec_000"] .capture-duration')
            ).to_have_text("60.00 s")
            expect(
                page.locator('[data-recording-id="rec_000"] .capture-doppler-summary')
            ).to_contain_text("123.0 Hz/s")
            expect(
                page.locator('[data-recording-id="rec_000"] .pilot-detection-counts')
            ).to_have_text("2 / 1")
            expect(
                page.locator('[data-recording-id="rec_000"] .satellites-tracked')
            ).to_have_text("3")

            api_urls = [url for url in requested if urlparse(url).path.startswith("/api/")]
            api_paths = [urlparse(url).path for url in api_urls]
            assert api_paths.count("/api/captures") == 1
            assert set(api_paths) == {
                "/api/captures",
                "/api/activity",
                "/api/recordings",
                "/api/tracks",
                "/api/storage-health",
            }
            assert not [url for url in api_urls if urlparse(url).path.startswith("/api/v")]
            assert not failed
            assert not bad_responses
            assert not console_errors
            assert not page_errors
            capture_requests = [item for item in api.requests if item.path == "/api/captures"]
            assert len(capture_requests) == 1
            capture_url = next(url for url in api_urls if urlparse(url).path == "/api/captures")
            assert parse_qs(urlparse(capture_url).query) == {
                "start_utc_ns": [str(_START_NS)],
                "stop_utc_ns": [str(_STOP_NS)],
                "maximum_recordings": ["100"],
            }
        finally:
            browser.close()


def test_master_capture_pagination_is_explicit_and_uses_same_snapshot_route() -> None:
    api = _SnapshotApi()
    with _running_dashboard(api) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=_browser_environment())
        try:
            page = browser.new_page()
            _freeze_clock(page)
            response = page.goto(base_url)
            assert response is not None and response.ok
            rows = page.locator("#capture-attempts-body tr")
            expect(rows).to_have_count(100)
            assert len([item for item in api.requests if item.path == "/api/captures"]) == 1
            load_more = page.get_by_role("button", name="Load more capture batches")
            expect(load_more).to_be_visible()
            load_more.click()
            expect(rows).to_have_count(102)
            capture_requests = [item for item in api.requests if item.path == "/api/captures"]
            assert len(capture_requests) == 2
            assert capture_requests[1].query["cursor"] == "page-2"
            assert capture_requests[1].query["maximum_recordings"] == "100"
            expect(load_more).to_be_hidden()
        finally:
            browser.close()
