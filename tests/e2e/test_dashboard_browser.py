from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page, expect, sync_playwright

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.dashboard.api import DashboardJsonApplicationV2
from leo_flow.dashboard.repository import InMemoryDashboardRepository
from leo_flow.dashboard.ui import DashboardUiApplication
from tests.dashboard._fixtures import (
    BATCH_EXCESSIVE_SKEW,
    BATCH_PEER_FAILED,
    BATCH_PENDING,
    BATCH_READY,
    EVALUATION_ID,
    GAUSS_BATCH_20_21,
    GAUSS_RADIO_15,
    GAUSS_RADIO_20,
    GAUSS_RADIO_21,
    gauss_three_radio_repository,
    repository,
)

_BROWSER_NOW_MS = 1_000


def _distribution_payload() -> str:
    methods = ("anchor-8", "glrt-32")
    distributions = []
    for method_index, method in enumerate(methods):
        for kind_index, score_kind in enumerate(("qin", "surrogate")):
            counts = [0] * 40
            point_count = 12 if score_kind == "qin" else 48
            counts[8 + method_index * 8 - kind_index * 4] = point_count
            distributions.append(
                {
                    "method": method,
                    "radio_id": "radio_a",
                    "receiver_chain_id": "rx_lnb_a",
                    "edge": "upper",
                    "score_kind": score_kind,
                    "recording_count": 3,
                    "point_count": point_count,
                    "mean": 0.2125 + method_index * 0.2 - kind_index * 0.1,
                    "standard_deviation": 0.01,
                    "minimum": 0.2 + method_index * 0.2 - kind_index * 0.1,
                    "maximum": 0.225 + method_index * 0.2 - kind_index * 0.1,
                    "bins": [
                        {
                            "index": index,
                            "lower": index / 40,
                            "upper": (index + 1) / 40,
                            "count": count,
                            "density": count / point_count * 40,
                        }
                        for index, count in enumerate(counts)
                    ],
                }
            )
    return json.dumps(
        {
            "schema_version": 1,
            "start_utc_ns": 1,
            "stop_utc_ns": 2,
            "score_domain_lower": 0.0,
            "score_domain_upper": 1.0,
            "bin_count": 40,
            "recording_count": 3,
            "truncated": False,
            "point_identity": "recording+segment+radio+receiver-chain+edge+method+pattern",
            "distributions": distributions,
            "warnings": [
                "finite-surrogate-ensemble-not-calibrated-null-distribution",
                "candidate-evidence-not-detection",
            ],
        }
    )


@contextmanager
def _running_dashboard(
    queries: InMemoryDashboardRepository | None = None,
    capture_batches: InMemoryDashboardRepository | None = None,
) -> Iterator[str]:
    """Run the real loopback HTTP adapter until the browser test is complete."""

    server = StdlibDashboardServer(request_timeout_s=0.01)
    server.preflight("127.0.0.1", 0)
    queries = queries or repository(50)
    application = DashboardUiApplication(
        DashboardJsonApplicationV2(queries, capture_batches or queries)
    )
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            server.serve_once(application)

    worker = threading.Thread(target=serve, name="dashboard-e2e-server")
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.bound_port}"
    finally:
        stop.set()
        worker.join(timeout=1)
        server.close(1)
        assert not worker.is_alive()


def _freeze_browser_clock(page: Page) -> None:
    page.add_init_script(
        f"""
        (() => {{
          const fixedNow = {_BROWSER_NOW_MS};
          const NativeDate = Date;
          class FixedDate extends NativeDate {{
            constructor(...args) {{
              super(...(args.length === 0 ? [fixedNow] : args));
            }}
            static now() {{ return fixedNow; }}
          }}
          FixedDate.parse = NativeDate.parse;
          FixedDate.UTC = NativeDate.UTC;
          globalThis.Date = FixedDate;
        }})();
        """
    )


def _browser_environment() -> dict[str, str | float | bool]:
    environment: dict[str, str | float | bool] = dict(os.environ)
    root = Path.home() / ".cache" / "ms-playwright" / "ubuntu-libs"
    if root.is_dir():
        environment["LD_LIBRARY_PATH"] = str(root / "usr/lib/x86_64-linux-gnu")
        environment["FONTCONFIG_FILE"] = str(root / "etc/fonts/fonts.conf")
        environment["FONTCONFIG_SYSROOT"] = str(root)
    return environment


def test_operator_dashboard_end_to_end_in_a_real_browser() -> None:
    with (
        _running_dashboard(repository(50), repository(2)) as base_url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch(headless=True, env=_browser_environment())
        page = browser.new_page()
        _freeze_browser_clock(page)
        page.route(
            "**/api/v5/capture-attempts/cattempt_ready_a/radio-lifecycle",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=(
                    '{"schema":{"schema_id":"org.leo-flow.dashboard.capture-attempt-radio-lifecycle",'
                    '"version":{"major":0,"minor":1}},"attempt_id":"cattempt_ready_a",'
                    '"radio_id":"radio_a","reason":"radio_rebooted","confidence":"high",'
                    '"evidence_codes":["boot_id_changed"],'
                    '"preflight_boot_id":"41974bfd-7aa8-4d28-b1c8-57d21c3e05bb",'
                    '"terminal_boot_id":"d6f89d3a-6856-441f-83db-96c71728e15b",'
                    '"preflight_uptime_ns":100,"terminal_uptime_ns":10,'
                    '"observer_available_at_terminal":true}'
                ),
            ),
        )

        response = page.goto(base_url)
        assert response is not None and response.ok
        expect(page).to_have_title("LEO Flow · Operator dashboard")
        expect(page.locator("#app-status")).to_have_attribute("data-state", "ready")
        expect(page.locator("#app-status-text")).to_have_text(
            "Current catalog views loaded"
        )

        activity = page.locator("#activity-table tbody tr")
        expect(activity).to_have_count(2)
        expect(activity.nth(0).locator("th, td")).to_have_text(
            ["radio_a", "2", "2", "4"]
        )
        expect(activity.nth(1).locator("th, td")).to_have_text(
            ["radio_b", "0", "1", "1"]
        )

        expect(page.locator("#capture-batches-state")).to_have_attribute(
            "data-state", "ready"
        )
        captures = page.locator("#capture-attempts-body tr")
        expect(captures).to_have_count(4)
        load_more = page.get_by_role("button", name="Load more capture batches")
        expect(load_more).to_be_visible()
        with page.expect_response(
            lambda response: (
                "/api/v2/capture-batches?" in response.url and "cursor=" in response.url
            )
        ) as next_page:
            load_more.click()
        assert next_page.value.request.method == "GET"
        expect(captures).to_have_count(8)
        expect(load_more).to_be_hidden()
        expect(page.locator("#capture-batches-state")).to_contain_text(
            "All matching batches are loaded"
        )

        ready = page.locator(f'[data-batch-id="{BATCH_READY}"]')
        expect(ready).to_have_count(2)
        ready_a = page.locator('[data-attempt-id="cattempt_ready_a"]')
        ready_b = page.locator('[data-attempt-id="cattempt_ready_b"]')
        expect(ready_a.locator("td").first).to_have_text("radio_a")
        expect(ready_b.locator("td").first).to_have_text("radio_b")
        expect(ready_a.locator(".capture-status-icon")).to_have_text("✓")
        expect(ready_a.locator(".analysis-status-icon")).to_have_attribute(
            "data-state", "complete"
        )
        expect(ready_a.locator(".pilot-detection-counts")).to_have_text("— / —")
        expect(ready_a).to_have_attribute("tabindex", "0")
        expect(ready_a).to_have_attribute(
            "aria-label",
            "View capture details, waterfall, and analysis for rec_ready_a",
        )

        pending = page.locator(f'[data-batch-id="{BATCH_PENDING}"]')
        expect(pending).to_have_count(2)
        pending_b = page.locator('[data-attempt-id="cattempt_pending_b"]')
        expect(pending_b).to_contain_text("pending")
        expect(pending_b).to_contain_text("unavailable")

        peer_failed = page.locator(f'[data-batch-id="{BATCH_PEER_FAILED}"]')
        expect(peer_failed).to_have_count(2)
        failed = page.locator('[data-attempt-id="cattempt_peer_failed_b"]')
        preserved = page.locator('[data-attempt-id="cattempt_peer_failed_a"]')
        expect(preserved).to_have_attribute("tabindex", "0")
        expect(failed.locator(".capture-status-icon")).to_have_text("✕")
        expect(failed.locator(".capture-status-icon")).to_have_attribute(
            "data-state", "failed"
        )
        expect(failed.locator("a")).to_have_count(0)
        expect(failed).not_to_have_attribute("tabindex", "0")
        failed.locator("td").first.click()
        expect(page).to_have_url(base_url + "/")

        page.locator("#capture-radio-filter").fill("radio_a")
        with page.expect_response(
            lambda response: (
                "/api/v2/capture-batches?" in response.url and "cursor=" in response.url
            )
        ):
            page.get_by_role("button", name="Apply capture filters").click()
        expect(captures).to_have_count(4)
        expect(
            page.locator('#capture-attempts-body tr[data-radio-id="radio_a"]')
        ).to_have_count(4)
        expect(
            page.locator('#capture-attempts-body tr[data-radio-id="radio_b"]')
        ).to_have_count(0)
        expect(page.locator("#capture-batches-state")).to_contain_text(
            "All matching batches are loaded"
        )

        page.locator("#capture-radio-filter").fill("radio_absent")
        page.get_by_role("button", name="Apply capture filters").click()
        expect(captures).to_have_count(0)
        expect(page.locator("#capture-batches-state")).to_have_attribute(
            "data-state", "empty"
        )
        expect(page.locator("#capture-batches-state")).to_contain_text(
            "No captures match this radio filter"
        )

        page.get_by_role("button", name="Clear radio").click()
        expect(captures).to_have_count(8)

        excessive_skew = page.locator(f'[data-batch-id="{BATCH_EXCESSIVE_SKEW}"]')
        expect(excessive_skew).to_have_count(2)
        expect(excessive_skew.first.locator(".capture-status-icon")).to_have_text("✓")

        ready_a.locator("td").first.click()
        expect(page).to_have_url(f"{base_url}/recordings/rec_ready_a")
        expect(page).to_have_title("Capture detail · LEO Flow")
        expect(page.locator("#capture-identity")).to_have_text("rec_ready_a")
        expect(page.locator("#capture-facts-heading")).to_be_visible()
        expect(page.locator("#waterfall-state")).to_be_visible()
        expect(page.locator("#waterfall-canvas")).to_be_attached()
        expect(page.locator("#starlink-decision")).to_be_visible()
        expect(page.locator("#diagnostic-features")).not_to_have_attribute("open", "")
        page.go_back()
        expect(page.locator("#app-status")).to_have_attribute("data-state", "ready")

        ready_a.focus()
        page.keyboard.press("Enter")
        expect(page).to_have_url(f"{base_url}/recordings/rec_ready_a")
        page.go_back()
        expect(page.locator("#app-status")).to_have_attribute("data-state", "ready")

        recordings = page.locator("#recordings-table tbody tr")
        expect(recordings).to_have_count(4)
        expect(page.locator("#recordings-state")).to_have_attribute(
            "data-state", "ready"
        )
        expect(page.get_by_role("button", name="rec_1", exact=True)).to_be_visible()

        page.get_by_role("button", name="rec_1", exact=True).click()
        expect(page.locator("#recording-detail")).to_have_attribute(
            "data-state", "ready"
        )
        expect(page.locator("#recording-detail-facts")).to_contain_text("Available")
        expect(page.locator("#features-state")).to_have_attribute("data-state", "ready")
        expect(page.locator("#features-list li")).to_have_count(3)
        expect(page.locator("#features-list")).to_contain_text("glrt32")
        expect(page.locator("#features-list")).to_contain_text("coarse-E")

        page.get_by_role("button", name="rec_4", exact=True).click()
        expect(page.locator("#recording-detail")).to_have_attribute(
            "data-state", "missing"
        )
        expect(page.locator("#recording-detail-state")).to_contain_text(
            "missing or unavailable"
        )

        expect(page.locator("#tracks-state")).to_have_attribute("data-state", "ready")
        expect(page.locator("#tracks-list li")).to_have_count(3)
        page.get_by_role("button", name="Load model model_a").first.click()
        expect(page.locator("#model-state")).to_have_attribute("data-state", "ready")
        expect(page.locator("#model-facts")).to_contain_text("production")

        page.locator("#evaluation-id").fill(str(EVALUATION_ID))
        page.locator("#evaluation-form").get_by_role(
            "button", name="Load summary"
        ).click()
        expect(page.locator("#evaluation-state")).to_have_attribute(
            "data-state", "partial"
        )
        expect(page.locator("#evaluation-state")).to_contain_text("fixture warning")
        expect(page.locator("#evaluation-body tr")).to_have_count(3)

        page.locator("#evaluation-id").fill("eval_missing")
        page.locator("#evaluation-form").get_by_role(
            "button", name="Load summary"
        ).click()
        expect(page.locator("#evaluation-state")).to_have_attribute(
            "data-state", "error"
        )
        expect(page.locator("#evaluation-state")).to_contain_text("was not found")
        expect(page.locator("#evaluation-state")).to_have_attribute("role", "status")

        expect(page.locator("#storage-state")).to_have_attribute("data-state", "ready")
        expect(page.locator("#storage-meter")).to_be_visible()
        expect(page.locator("#storage-free-label")).to_have_text("250 B free")
        expect(page.locator("#storage-total-label")).to_have_text("1000 B total")

        browser.close()


def test_aggregate_score_density_page_in_a_real_browser() -> None:
    with _running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=_browser_environment())
        try:
            page = browser.new_page()
            _freeze_browser_clock(page)
            requests: list[str] = []

            def fulfill_distributions(route) -> None:
                requests.append(route.request.url)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_distribution_payload(),
                )

            page.route(
                "**/api/v12/surrogate-score-distributions?*",
                fulfill_distributions,
            )
            response = page.goto(f"{base_url}/aggregate-stats")
            assert response is not None and response.ok
            expect(page).to_have_title("LEO Flow · Aggregate statistics")
            expect(page.locator("#aggregate-status")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#score-summary-body tr")).to_have_count(4)
            expect(page.locator("#method-selector input:checked")).to_have_count(2)
            expect(page.locator("#density-canvas")).to_be_visible()
            assert page.locator("#density-canvas").evaluate(
                "canvas => canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data.some(value => value !== 0)"
            )
            page.get_by_label("anchor-8").uncheck()
            expect(page.locator("#method-selector input:checked")).to_have_count(1)
            page.get_by_label("Precommitted surrogate scores").uncheck()
            expect(page.locator("#score-summary-body tr")).to_have_count(1)
            page.locator("#density-window-hours").select_option("24")
            page.get_by_role("button", name="Refresh").click()
            expect(page.locator("#aggregate-status")).to_have_attribute(
                "data-state", "ready"
            )
            assert len(requests) == 2
        finally:
            browser.close()


def test_gauss_single_and_dual_radio_results_share_the_real_dashboard() -> None:
    queries = gauss_three_radio_repository()
    with _running_dashboard(queries) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=_browser_environment())
        try:
            page = browser.new_page()
            _freeze_browser_clock(page)
            responses: list[str] = []
            page.on(
                "response",
                lambda response: responses.append(response.url.removeprefix(base_url)),
            )

            response = page.goto(base_url)
            assert response is not None and response.ok
            expect(page.locator("#app-status")).to_have_attribute("data-state", "ready")

            activity = page.locator("#activity-table tbody")
            expect(activity.locator("tr")).to_have_count(3)
            expect(activity).to_contain_text(str(GAUSS_RADIO_15))
            expect(activity).to_contain_text(str(GAUSS_RADIO_20))
            expect(activity).to_contain_text(str(GAUSS_RADIO_21))

            recordings = page.locator("#recordings-table tbody")
            expect(recordings.locator("tr")).to_have_count(3)
            expect(recordings).to_contain_text(str(GAUSS_RADIO_15))
            expect(recordings).to_contain_text(str(GAUSS_RADIO_20))
            expect(recordings).to_contain_text(str(GAUSS_RADIO_21))

            batch = page.locator(f'[data-batch-id="{GAUSS_BATCH_20_21}"]')
            expect(batch).to_have_count(2)
            radio_20_row = page.locator(
                f'#capture-attempts-body tr[data-radio-id="{GAUSS_RADIO_20}"]'
            )
            radio_21_row = page.locator(
                f'#capture-attempts-body tr[data-radio-id="{GAUSS_RADIO_21}"]'
            )
            expect(radio_20_row).to_have_count(1)
            expect(radio_21_row).to_have_count(1)
            expect(radio_20_row.locator("td").first).to_have_text(".20 / 5d4d")
            expect(radio_21_row.locator("td").first).to_have_text(".21 / 19f2")
            expect(radio_20_row.locator(".capture-status-icon")).to_have_text("✓")
            expect(radio_20_row.locator(".analysis-status-icon")).to_have_attribute(
                "data-state", "complete"
            )
            expect(
                page.locator('#capture-radio-options option[value=".20"]')
            ).to_have_attribute("label", f"192.168.1.20 · {GAUSS_RADIO_20}")

            for filter_text, radio in (
                (str(GAUSS_RADIO_20), GAUSS_RADIO_20),
                (".20", GAUSS_RADIO_20),
                ("192.168.1.20", GAUSS_RADIO_20),
                (str(GAUSS_RADIO_21), GAUSS_RADIO_21),
                (".21", GAUSS_RADIO_21),
                ("192.168.1.21", GAUSS_RADIO_21),
            ):
                page.locator("#capture-radio-filter").fill(filter_text)
                page.get_by_role("button", name="Apply capture filters").click()
                filtered = page.locator("#capture-attempts-body tr")
                expect(filtered).to_have_count(1)
                expect(filtered).to_have_attribute("data-radio-id", str(radio))

            page.locator("#capture-radio-filter").fill(str(GAUSS_RADIO_15))
            page.get_by_role("button", name="Apply capture filters").click()
            expect(page.locator("#capture-attempts-body tr")).to_have_count(0)
            expect(page.locator("#capture-batches-state")).to_contain_text(
                "No captures match this radio filter"
            )
            page.get_by_role("button", name="Clear radio").click()
            expect(page.locator("#capture-attempts-body tr")).to_have_count(2)

            for recording_id in (
                "rec_gauss_15",
                "rec_gauss_20",
                "rec_gauss_21",
            ):
                page.get_by_role("button", name=recording_id, exact=True).click()
                expect(page.locator("#recording-detail")).to_have_attribute(
                    "data-state", "ready"
                )
                expect(page.locator("#features-state")).to_have_attribute(
                    "data-state", "ready"
                )
                expect(page.locator("#features-list")).to_contain_text(
                    "gauss-quality-v1"
                )

            assert any(path.startswith("/api/activity?") for path in responses)
            assert any(path.startswith("/api/recordings?") for path in responses)
            assert any(
                path.startswith("/api/v2/capture-batches?") for path in responses
            )
            assert all(not path.startswith("data:") for path in responses)

            for option, hours in (("8", 8), ("720", 720)):
                page.locator("#window-hours").select_option(option)
                with page.expect_response(
                    lambda candidate: "/api/v2/capture-batches?" in candidate.url
                ) as refreshed:
                    page.get_by_role("button", name="Refresh", exact=True).click()
                request = refreshed.value.request
                bounds = parse_qs(urlsplit(request.url).query)
                assert request.method == "GET"
                assert (
                    int(bounds["stop_utc_ns"][0]) - int(bounds["start_utc_ns"][0])
                    == hours * 3_600_000_000_000
                )
                expect(page.locator("#app-status")).to_have_attribute(
                    "data-state", "ready"
                )

            page.locator("#capture-window-hours").select_option("8")
            with page.expect_response(
                lambda candidate: "/api/v2/capture-batches?" in candidate.url
            ) as capture_filtered:
                page.get_by_role("button", name="Apply capture filters").click()
            capture_bounds = parse_qs(urlsplit(capture_filtered.value.url).query)
            assert (
                int(capture_bounds["stop_utc_ns"][0])
                - int(capture_bounds["start_utc_ns"][0])
                == 8 * 3_600_000_000_000
            )
        finally:
            browser.close()
