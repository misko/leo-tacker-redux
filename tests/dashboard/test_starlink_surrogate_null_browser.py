from __future__ import annotations

import json
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Route, expect, sync_playwright

from leo_flow.contracts.core import RadioId, UtcNs, canonical_json_bytes
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod
from leo_flow.contracts.starlink_surrogate_null_pipeline import (
    StarlinkSurrogateNullQueryV0_1,
    StarlinkSurrogateNullRecordingState,
)
from tests.dashboard.test_doppler_visualization_browser import (
    browser_environment,
    running_dashboard,
)
from tests.dashboard.test_starlink_surrogate_null_api import (
    RECORDING_ID,
    view,
)


def _query_from_url(url: str) -> StarlinkSurrogateNullQueryV0_1:
    values = parse_qs(urlparse(url).query)
    return StarlinkSurrogateNullQueryV0_1(
        RECORDING_ID,
        (StarlinkDetectorMethod(values["methods"][0]),),
        () if "radio_ids" not in values else (RadioId(values["radio_ids"][0]),),
        () if "channel_numbers" not in values else (int(values["channel_numbers"][0]),),
        () if "edges" not in values else (StarlinkEdge(values["edges"][0]),),
        None
        if "interval_start_utc_ns" not in values
        else UtcNs(int(values["interval_start_utc_ns"][0])),
        None
        if "interval_stop_utc_ns" not in values
        else UtcNs(int(values["interval_stop_utc_ns"][0])),
        int(values["maximum_rows"][0]),
    )


def test_browser_renders_every_surrogate_and_sends_bounded_filters() -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1400})
            queries: list[StarlinkSurrogateNullQueryV0_1] = []

            def fulfill_surrogate(route: Route) -> None:
                query = _query_from_url(route.request.url)
                queries.append(query)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(json.loads(canonical_json_bytes(view(query)))),
                )

            page.route(
                f"**/api/recordings/{RECORDING_ID}/starlink-surrogate-null?*",
                fulfill_surrogate,
            )
            response = page.goto(f"{base_url}/recordings/{RECORDING_ID}")
            assert response is not None and response.ok
            page.locator("#evidence-load-extended").click()

            expect(page.locator("#surrogate-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#surrogate-warning")).to_be_visible()
            expect(page.locator("#surrogate-warning")).to_contain_text(
                "not a calibrated p-value"
            )
            expect(page.locator("#surrogate-warning")).to_contain_text(
                "not a Starlink detection"
            )
            expect(page.locator("#surrogate-summary-facts")).to_contain_text(
                "Mean Qin score"
            )
            expect(page.locator("#surrogate-summary-facts")).to_contain_text("0.800000")
            row = page.locator(".surrogate-row-card")
            expect(row).to_have_count(1)
            expect(row).to_contain_text("Finite upper-tail rank")
            expect(row).to_contain_text("0.400000 · (1 + 1) / 5")
            expect(row.locator(".surrogate-score-row")).to_have_count(5)
            expect(row.locator('[data-role="qin"]')).to_contain_text("Qin exact")
            expect(row.locator('[data-role="qin"]')).to_contain_text("0.800000")
            expect(row.locator('[data-role="surrogate"]')).to_have_count(4)
            expect(row.locator('[data-role="surrogate"]').nth(2)).to_contain_text(
                "0.900000"
            )
            row.locator("details").click()
            expect(row.locator("details")).to_contain_text("codebook 0")
            expect(row.locator("details")).to_contain_text("seed 10000")
            expect(row.locator("details")).to_contain_text(
                "surrogate-null-analyzer 0.1.0"
            )
            expect(row.locator("details")).to_contain_text("Environment digest")

            page.locator("#surrogate-method").select_option("glrt-32")
            expect(row.locator(".status-badge")).to_have_text("glrt-32")
            page.locator("#surrogate-radio").select_option("radio_surrogate")
            page.locator("#surrogate-channel").select_option("4")
            page.locator("#surrogate-edge").select_option("lower")
            page.locator("#surrogate-start").fill("2026-08-17T12:00")
            page.locator("#surrogate-stop").fill("2026-08-17T12:01")
            page.locator("#surrogate-apply-time").click()
            expect(page.locator("#surrogate-state")).to_have_attribute(
                "data-state", "ready"
            )

            selected = queries[-1]
            assert selected.methods == (StarlinkDetectorMethod.GLRT_32,)
            assert selected.radio_ids == (RadioId("radio_surrogate"),)
            assert selected.channel_numbers == (4,)
            assert selected.edges == (StarlinkEdge.LOWER,)
            assert selected.interval_start_utc_ns is not None
            assert selected.interval_stop_utc_ns is not None
            assert selected.interval_stop_utc_ns > selected.interval_start_utc_ns
            assert selected.maximum_rows == 64
        finally:
            browser.close()


def test_browser_exposes_pending_until_the_projection_completes() -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        held: list[Route] = []
        try:
            page = browser.new_page(viewport={"width": 1200, "height": 900})

            def hold_surrogate(route: Route) -> None:
                held.append(route)

            page.route(
                f"**/api/recordings/{RECORDING_ID}/starlink-surrogate-null?*",
                hold_surrogate,
            )
            response = page.goto(f"{base_url}/recordings/{RECORDING_ID}")
            assert response is not None and response.ok
            page.locator("#evidence-load-extended").click()

            expect(page.locator("#surrogate-state")).to_have_attribute(
                "data-state", "pending"
            )
            expect(page.locator("#surrogate-summary")).to_be_hidden()
            assert len(held) == 1

            query = _query_from_url(held[0].request.url)
            held[0].fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(json.loads(canonical_json_bytes(view(query)))),
            )
            expect(page.locator("#surrogate-state")).to_have_attribute(
                "data-state", "ready"
            )
        finally:
            browser.close()


def test_browser_exposes_explicit_not_evaluated_state() -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1200, "height": 900})

            def fulfill_surrogate(route: Route) -> None:
                query = _query_from_url(route.request.url)
                payload = replace(
                    view(query),
                    state=StarlinkSurrogateNullRecordingState.NOT_EVALUATED,
                    total_matching_rows=0,
                    rows=(),
                    aggregates=(),
                )
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(json.loads(canonical_json_bytes(payload))),
                )

            page.route(
                f"**/api/recordings/{RECORDING_ID}/starlink-surrogate-null?*",
                fulfill_surrogate,
            )
            response = page.goto(f"{base_url}/recordings/{RECORDING_ID}")
            assert response is not None and response.ok
            page.locator("#evidence-load-extended").click()

            expect(page.locator("#surrogate-state")).to_have_attribute(
                "data-state", "not-evaluated"
            )
            expect(page.locator("#surrogate-state")).to_contain_text("not evaluated")
            expect(page.locator("#surrogate-warning")).to_be_visible()
            expect(page.locator("#surrogate-row-count")).to_have_text(
                "0 shown · 0 matching"
            )
        finally:
            browser.close()


@pytest.mark.parametrize(
    ("status", "expected_state", "expected_text"),
    [
        (404, "unavailable", "unavailable for this recording"),
        (500, "error", "failed"),
    ],
)
def test_browser_exposes_unavailable_and_error_states(
    status: int,
    expected_state: str,
    expected_text: str,
) -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1200, "height": 900})

            def fulfill_surrogate(route: Route) -> None:
                route.fulfill(
                    status=status,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "error": {
                                "code": "not_found"
                                if status == 404
                                else "internal_error",
                                "message": "test response",
                            }
                        }
                    ),
                )

            page.route(
                f"**/api/recordings/{RECORDING_ID}/starlink-surrogate-null?*",
                fulfill_surrogate,
            )
            response = page.goto(f"{base_url}/recordings/{RECORDING_ID}")
            assert response is not None and response.ok
            page.locator("#evidence-load-extended").click()

            expect(page.locator("#surrogate-state")).to_have_attribute(
                "data-state", expected_state
            )
            expect(page.locator("#surrogate-state")).to_contain_text(expected_text)
            expect(page.locator("#surrogate-warning")).to_be_hidden()
            expect(page.locator("#surrogate-summary")).to_be_hidden()
            expect(page.locator("#surrogate-rows")).to_be_empty()
        finally:
            browser.close()
