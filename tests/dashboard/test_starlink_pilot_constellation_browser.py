from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Route, expect, sync_playwright

from leo_flow.contracts.core import ReceiverChainId, SegmentId, canonical_json_bytes
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_pilot_constellation_pipeline import (
    StarlinkPilotConstellationQueryV0_1,
)
from tests.dashboard.test_doppler_visualization_browser import (
    browser_environment,
    running_dashboard,
)
from tests.dashboard.test_starlink_pilot_constellation_api import (
    RECORDING_ID,
    view,
)


def _query_from_url(url: str) -> StarlinkPilotConstellationQueryV0_1:
    values = parse_qs(urlparse(url).query)
    return StarlinkPilotConstellationQueryV0_1(
        RECORDING_ID,
        ()
        if "segment_ids" not in values
        else tuple(SegmentId(item) for item in values["segment_ids"][0].split(",")),
        ()
        if "receiver_chain_ids" not in values
        else tuple(
            ReceiverChainId(item) for item in values["receiver_chain_ids"][0].split(",")
        ),
        ()
        if "edges" not in values
        else tuple(StarlinkEdge(item) for item in values["edges"][0].split(",")),
        int(values["maximum_streams"][0]),
        int(values["maximum_points_per_stream"][0]),
    )


def test_browser_renders_qin_constellation_facts_and_selected_stream_filters() -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1500})
            queries: list[StarlinkPilotConstellationQueryV0_1] = []

            def fulfill_constellation(route: Route) -> None:
                query = _query_from_url(route.request.url)
                queries.append(query)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(json.loads(canonical_json_bytes(view(query)))),
                )

            page.route(
                f"**/api/v11/recordings/{RECORDING_ID}/starlink-pilot-constellation?*",
                fulfill_constellation,
            )
            response = page.goto(f"{base_url}/recordings/{RECORDING_ID}")
            assert response is not None and response.ok
            page.locator("#evidence-load-extended").click()

            expect(page.locator("#constellation-state")).to_have_attribute(
                "data-state", "ready"
            )
            warning = page.locator("#constellation-warning")
            expect(warning).to_be_visible()
            expect(warning).to_contain_text("Known synchronization pilot")
            expect(warning).to_contain_text("No user payload is decoded")
            expect(warning).to_contain_text("not a calibrated Starlink detection")
            card = page.locator(".constellation-card")
            expect(card).to_have_count(1)
            expect(card).to_contain_text("Hard-symbol accuracy")
            expect(card).to_contain_text("91.000%")
            expect(card).to_contain_text("RMS EVM")
            expect(card).to_contain_text("0.180000")
            expect(card).to_contain_text("Model SNR diagnostic")
            expect(card).to_contain_text("14.200 dB")
            expect(card).to_contain_text("Residual CFO refinement")
            expect(card).to_contain_text("4.5 Hz")
            expect(card).to_contain_text("Mean confidence")
            expect(card).to_contain_text("Mean entropy")
            expect(card.locator(".constellation-legend-item")).to_have_count(4)
            expect(card.locator("figcaption")).to_contain_text(
                "crosses mark ideal rotated-QPSK states"
            )
            canvas = card.locator("canvas")
            expect(canvas).to_have_attribute("data-rendered-points", "2400")
            expect(canvas).to_have_attribute(
                "aria-label",
                re.compile("known Qin synchronization-pilot", re.IGNORECASE),
            )
            expect(card.locator("tbody tr")).to_have_count(8)
            expect(card.locator("tbody tr").first).to_contain_text("528")

            page.locator("#constellation-segment").select_option("seg_constellation")
            page.locator("#constellation-receiver").select_option("rx_constellation")
            page.locator("#constellation-edge").select_option("upper")
            expect(page.locator("#constellation-state")).to_have_attribute(
                "data-state", "ready"
            )
            selected = queries[-1]
            assert selected.segment_ids == (SegmentId("seg_constellation"),)
            assert selected.receiver_chain_ids == (ReceiverChainId("rx_constellation"),)
            assert selected.edges == (StarlinkEdge.UPPER,)
            assert selected.maximum_streams == 16
            assert selected.maximum_points_per_stream == 2_400
            expect(page.locator(".constellation-card h3")).to_contain_text("upper edge")
        finally:
            browser.close()


def test_browser_exposes_pending_until_constellation_projection_completes() -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        held: list[Route] = []
        try:
            page = browser.new_page(viewport={"width": 1200, "height": 900})
            page.route(
                f"**/api/v11/recordings/{RECORDING_ID}/starlink-pilot-constellation?*",
                lambda route: held.append(route),
            )
            response = page.goto(f"{base_url}/recordings/{RECORDING_ID}")
            assert response is not None and response.ok
            page.locator("#evidence-load-extended").click()

            expect(page.locator("#constellation-state")).to_have_attribute(
                "data-state", "pending"
            )
            expect(page.locator("#constellation-warning")).to_be_hidden()
            expect(page.locator("#constellation-streams")).to_be_empty()
            assert len(held) == 1

            query = _query_from_url(held[0].request.url)
            held[0].fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(json.loads(canonical_json_bytes(view(query)))),
            )
            expect(page.locator("#constellation-state")).to_have_attribute(
                "data-state", "ready"
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
def test_browser_exposes_constellation_unavailable_and_error_states(
    status: int,
    expected_state: str,
    expected_text: str,
) -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1200, "height": 900})

            def fulfill_constellation(route: Route) -> None:
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
                f"**/api/v11/recordings/{RECORDING_ID}/starlink-pilot-constellation?*",
                fulfill_constellation,
            )
            response = page.goto(f"{base_url}/recordings/{RECORDING_ID}")
            assert response is not None and response.ok
            page.locator("#evidence-load-extended").click()

            expect(page.locator("#constellation-state")).to_have_attribute(
                "data-state", expected_state
            )
            expect(page.locator("#constellation-state")).to_contain_text(expected_text)
            expect(page.locator("#constellation-warning")).to_be_hidden()
            expect(page.locator("#constellation-streams")).to_be_empty()
        finally:
            browser.close()


def test_browser_fails_closed_on_invalid_presentation_contract() -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1200, "height": 900})

            def fulfill_unsafe(route: Route) -> None:
                query = _query_from_url(route.request.url)
                payload = json.loads(canonical_json_bytes(view(query)))
                payload["streams"][0]["original_point_count"] = 2_399
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(payload),
                )

            page.route(
                f"**/api/v11/recordings/{RECORDING_ID}/starlink-pilot-constellation?*",
                fulfill_unsafe,
            )
            response = page.goto(f"{base_url}/recordings/{RECORDING_ID}")
            assert response is not None and response.ok
            page.locator("#evidence-load-extended").click()

            expect(page.locator("#constellation-state")).to_have_attribute(
                "data-state", "error"
            )
            expect(page.locator("#constellation-state")).to_contain_text(
                "unsafe pilot-constellation semantics"
            )
            expect(page.locator("#constellation-warning")).to_be_hidden()
            expect(page.locator("#constellation-streams")).to_be_empty()
        finally:
            browser.close()
