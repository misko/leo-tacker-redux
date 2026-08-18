from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

from tests.dashboard.test_doppler_visualization_browser import (
    browser_environment,
    running_dashboard,
)
from tests.e2e.test_dashboard_browser import _freeze_browser_clock


def _payload(recording: str) -> dict[str, object]:
    return {
        "recording_id": recording,
        "mode": "overall",
        "candidate_only": True,
        "calibration_required": True,
        "analysis_ref": {"artifact_id": f"qam_{recording}"},
        "streams": [
            {
                "radio_id": "radio_a",
                "lnb_id": "lnb_a1",
                "receiver_chain_id": "rx_a1",
                "segment_id": "seg_a1",
                "edge": "lower",
                "overall": {
                    "support_weighted_hard_symbol_accuracy": 0.80,
                    "support_weighted_rms_evm": 0.78,
                    "window_count": 32,
                },
                "windows": [],
            },
            {
                "radio_id": "radio_a",
                "lnb_id": "lnb_a2",
                "receiver_chain_id": "rx_a2",
                "segment_id": "seg_a2",
                "edge": "lower",
                "overall": {
                    "support_weighted_hard_symbol_accuracy": 0.26,
                    "support_weighted_rms_evm": 4.0,
                    "window_count": 32,
                },
                "windows": [],
            },
        ],
    }


def test_master_table_progressively_loads_qam_and_links_every_recording_detail() -> (
    None
):
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page()
            _freeze_browser_clock(page)
            requests: list[str] = []

            def fulfill(route):
                requests.append(route.request.url)
                recording = route.request.url.split("/recordings/")[1].split("/")[0]
                query = parse_qs(urlparse(route.request.url).query)
                assert query == {
                    "mode": ["overall"],
                    "maximum_streams": ["16"],
                    "maximum_windows_per_stream": ["1"],
                    "maximum_points_per_constellation": ["1"],
                }
                if recording == "rec_pending_a":
                    route.fulfill(
                        status=404,
                        content_type="application/json",
                        body=json.dumps({"error": {"message": "pending"}}),
                    )
                    return
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_payload(recording)),
                )

            page.route(
                "**/api/v17/recordings/*/starlink-acquired-constellation?*", fulfill
            )
            page.goto(base_url)
            ready = page.locator(
                '[data-recording-id="rec_ready_a"] .capture-qam-summary'
            )
            pending = page.locator(
                '[data-recording-id="rec_pending_a"] .capture-qam-summary'
            )
            expect(ready).to_have_attribute("data-state", "complete")
            expect(ready.locator(".capture-qam-candidate")).to_have_count(2)
            expect(ready).to_contain_text("lnb_a1 / rx_a1:")
            expect(ready).to_contain_text("lnb_a2 / rx_a2:")
            expect(ready.locator(".qam-detail-link")).to_have_attribute(
                "href", "/recordings/rec_ready_a#evidence-qam"
            )
            expect(pending).to_have_attribute("data-state", "pending")
            expect(pending).to_contain_text("Pending")
            expect(pending.locator(".qam-detail-link")).to_have_attribute(
                "href", "/recordings/rec_pending_a#evidence-qam"
            )
            assert len(requests) == 3
            assert (
                len({url.split("/recordings/")[1].split("/")[0] for url in requests})
                == 3
            )
        finally:
            browser.close()
