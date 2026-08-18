from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.dashboard.api import DashboardJsonApplicationV3
from leo_flow.dashboard.ui import DashboardUiApplication
from tests.dashboard._fixtures import repository
from tests.dashboard._recording_detail_fixtures import (
    RECORDING_ID,
    RecordingDetailFixtureQueries,
    starlink_candidates,
)


@contextmanager
def running_detail_dashboard(*, with_starlink: bool = False) -> Iterator[str]:
    server = StdlibDashboardServer(request_timeout_s=0.01)
    server.preflight("127.0.0.1", 0)
    queries = RecordingDetailFixtureQueries(
        repository(), starlink_candidates() if with_starlink else None
    )
    application = DashboardUiApplication(
        DashboardJsonApplicationV3(queries, queries, queries, queries, queries)
    )
    stopped = threading.Event()

    def serve() -> None:
        while not stopped.is_set():
            server.serve_once(application)

    worker = threading.Thread(target=serve, name="recording-detail-e2e-server")
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


def test_capture_detail_page_renders_tunings_analysis_and_projected_waterfall() -> None:
    with running_detail_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            responses: list[str] = []
            page.on(
                "response",
                lambda response: responses.append(response.url.removeprefix(base_url)),
            )
            response = page.goto(f"{base_url}/recordings/{RECORDING_ID}")
            assert response is not None and response.ok
            page.locator("#evidence-load-extended").click()

            expect(page).to_have_title(f"{RECORDING_ID} · Capture detail · LEO Flow")
            expect(page.locator("#capture-page-state")).to_have_attribute(
                "data-state", "ready"
            )
            facts = page.locator("#capture-detail-facts")
            expect(facts).to_contain_text("plan_dashboard")
            expect(facts).to_contain_text("station_gauss")
            expect(facts).to_contain_text("serial-19f2")
            expect(facts).to_contain_text("sha256")

            expect(page.locator("#segments-state")).to_have_attribute(
                "data-state", "ready"
            )
            segment = page.locator("#segments-body tr")
            expect(segment).to_have_count(1)
            expect(segment).to_contain_text("seg_dashboard")
            expect(segment).to_contain_text("10.755000 GHz")
            expect(segment).to_contain_text("5.000 MHz")
            expect(segment).to_contain_text("rx_a, rx_b")

            expect(page.locator("#waterfall-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#waterfall-figure")).to_be_visible()
            canvas = page.locator("#waterfall-canvas")
            expect(canvas).to_have_attribute(
                "aria-label",
                "Waterfall for seg_dashboard, receiver rx_a; "
                "2 time bins by 3 frequency bins",
            )
            distinct_pixels = page.evaluate(
                """() => {
                  const canvas = document.getElementById("waterfall-canvas");
                  const values = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
                  const colors = new Set();
                  for (let index = 0; index < values.length; index += 4000) {
                    colors.add(`${values[index]},${values[index + 1]},${values[index + 2]}`);
                  }
                  return colors.size;
                }"""
            )
            assert distinct_pixels > 1
            expect(page.locator("#waterfall-frequency-axis")).to_contain_text(
                "Frequency"
            )

            expect(page.locator("#analysis-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#starlink-decision-badge")).to_have_text(
                "Not evaluated"
            )
            expect(page.locator("#starlink-decision-summary")).to_contain_text(
                "not a zero-detection result"
            )
            diagnostics = page.locator("#diagnostic-features")
            expect(diagnostics).not_to_have_attribute("open", "")
            expect(page.locator("#analysis-table")).to_be_hidden()
            toggle = page.locator("#diagnostic-features-toggle")
            expect(toggle).to_have_attribute("aria-expanded", "false")
            expect(page.locator("#diagnostic-features-count")).to_have_text("3 rows")
            toggle.click()
            expect(toggle).to_have_attribute("aria-expanded", "true")
            expect(toggle).to_contain_text("Hide diagnostic features")
            expect(page.locator("#analysis-table")).to_be_visible()
            expect(page.locator("#analysis-body tr")).to_have_count(3)
            expect(page.locator("#analysis-body")).to_contain_text("Glrt32")
            expect(page.locator("#analysis-body")).to_contain_text("Coarse energy")
            exact = page.locator('#analysis-body tr[data-feature-id="feature_a"]')
            expect(exact).to_have_attribute("data-exact-score", "2")
            expect(exact).to_contain_text("Exact feature identity: feature_a.")

            assert f"/api/v3/recordings/{RECORDING_ID}" in responses
            assert f"/api/v3/recordings/{RECORDING_ID}/waterfall" in responses
            assert f"/api/v4/recordings/{RECORDING_ID}/starlink-suite" in responses
            assert f"/api/v3/recordings/{RECORDING_ID}/starlink" not in responses
            assert any(
                path.startswith(f"/api/recordings/{RECORDING_ID}/features?")
                for path in responses
            )
            assert (
                sum(
                    path.startswith(f"/api/recordings/{RECORDING_ID}/features?")
                    for path in responses
                )
                == 2
            )
            assert all(
                "cas:" not in path and "/home/" not in path for path in responses
            )
        finally:
            browser.close()


def test_capture_diagnostics_group_quality_and_psd_without_losing_identity() -> None:
    with running_detail_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page()
            page.route(
                f"**/api/recordings/{RECORDING_ID}/features?*",
                lambda route: route.fulfill(
                    json={
                        "items": [
                            {
                                "feature_id": "feature_aff3066dd38c44ef4707210ea33328d4",
                                "method_id": "compact-psd",
                                "score": 31.99999850628878,
                                "score_semantics": "peak-psd-to-median-psd-ratio",
                            },
                            {
                                "feature_id": "feature_b24d458979a413446b179622c275787d",
                                "method_id": "sample-quality",
                                "score": 25.264260428518387,
                                "score_semantics": "rms-magnitude-counts",
                            },
                        ],
                        "next_cursor": None,
                    }
                ),
            )
            response = page.goto(f"{base_url}/recordings/{RECORDING_ID}")
            assert response is not None and response.ok
            page.locator("#evidence-load-extended").click()

            expect(page.locator("#diagnostic-features-count")).to_have_text("2 rows")
            expect(page.locator("#analysis-table")).to_be_hidden()
            page.locator("#diagnostic-features-toggle").press("Enter")
            expect(page.locator("#analysis-table")).to_be_visible()
            body = page.locator("#analysis-body")
            expect(body).to_contain_text("Spectrum shape")
            expect(body).to_contain_text("Peak / median PSD")
            expect(body).to_contain_text("32 ×")
            expect(body).to_contain_text("Sample quality")
            expect(body).to_contain_text("RMS magnitude")
            expect(body).to_contain_text("25.2643 counts")
            exact = page.locator(
                '#analysis-body tr[data-feature-id="feature_aff3066dd38c44ef4707210ea33328d4"]'
            )
            expect(exact).to_have_attribute("data-exact-score", "31.99999850628878")
            expect(exact).to_contain_text(
                "Exact feature identity: feature_aff3066dd38c44ef4707210ea33328d4."
            )
        finally:
            browser.close()


def test_absent_waterfall_is_a_terminal_missing_state() -> None:
    with running_detail_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page()
            page.route(
                f"**/api/v3/recordings/{RECORDING_ID}/waterfall",
                lambda route: route.fulfill(
                    status=404,
                    json={
                        "error": {
                            "code": "not_found",
                            "message": "waterfall is absent",
                        }
                    },
                ),
            )
            response = page.goto(f"{base_url}/recordings/{RECORDING_ID}")
            assert response is not None and response.ok
            page.locator("#evidence-load-extended").click()
            expect(page.locator("#waterfall-state")).to_have_attribute(
                "data-state", "missing"
            )
            expect(page.locator("#waterfall-state")).to_contain_text(
                "has not been projected"
            )
        finally:
            browser.close()


def test_capture_detail_renders_candidates_without_a_detection_count() -> None:
    with (
        running_detail_dashboard(with_starlink=True) as base_url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            methods = [
                "anchor-8",
                "differential-16",
                "differential-32",
                "glrt-32",
                "glrt-64",
                "full-frame-acquire",
                "full-frame-verify",
                "full-frame-full",
            ]
            page.route(
                f"**/api/v4/recordings/{RECORDING_ID}/starlink-suite",
                lambda route: route.fulfill(
                    json={
                        "state": "candidates",
                        "analysis_ref": {
                            "artifact_id": "slsuite_browser",
                            "digest": {"algorithm": "sha256", "value": "a" * 64},
                            "schema": {
                                "schema_id": "org.leo-flow.starlink-detector-suite-recording-bundle",
                                "version": {"major": 0, "minor": 2},
                            },
                        },
                        "analyzed_stream_count": 1,
                        "method_count": 8,
                        "calibrated_detection_count": None,
                        "reason_codes": ["whole-search-calibration-required"],
                        "methods": [
                            {
                                "segment_id": "seg_dashboard",
                                "receiver_chain_id": "rx_a",
                                "edge": "lower",
                                "method": method,
                                "score": 0.75,
                                "control_score": 0.25,
                                "margin": 0.5,
                                "epoch_sample": 3,
                                "coarse_cfo_hz": 1000.0,
                                "residual_cfo_hz": 0.0,
                                "effective_search_cell_count": 583,
                                "frame_support": 4,
                            }
                            for method in methods
                        ],
                    }
                ),
            )
            response = page.goto(f"{base_url}/recordings/{RECORDING_ID}")
            assert response is not None and response.ok
            page.locator("#evidence-load-extended").click()
            expect(page.locator("#starlink-decision-badge")).to_have_text(
                "Candidates · uncalibrated"
            )
            expect(page.locator("#starlink-decision-summary")).to_contain_text(
                "no detection verdict or count exists"
            )
            expect(page.locator("#starlink-decision-facts")).to_contain_text(
                "Not available — calibration required"
            )
            candidates = page.locator("#starlink-candidates-body tr")
            expect(candidates).to_have_count(8)
            candidate_body = page.locator("#starlink-candidates-body")
            expect(candidate_body).to_contain_text("anchor-8")
            expect(candidate_body).to_contain_text("full-frame-full")
            expect(candidate_body).to_contain_text("0.500000")
            expect(page.locator("#diagnostic-features")).not_to_have_attribute(
                "open", ""
            )
        finally:
            browser.close()
