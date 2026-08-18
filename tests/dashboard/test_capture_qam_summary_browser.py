from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

from tests.dashboard.test_doppler_visualization_browser import (
    browser_environment,
    running_dashboard,
)
from tests.e2e.test_dashboard_browser import _freeze_browser_clock


def _candidate(
    recording: str, lnb: str, receiver: str, goodness: float
) -> dict[str, object]:
    return {
        "recording_id": recording,
        "radio_id": "radio_a",
        "lnb_id": lnb,
        "receiver_chain_id": receiver,
        "segment_id": f"seg_{receiver}",
        "edge": "lower",
        "qam_goodness": goodness,
        "hard_symbol_accuracy": 0.80 if goodness > 0.5 else 0.26,
        "rms_evm": 0.78 if goodness > 0.5 else 4.0,
        "window_count": 32,
        "analysis_id": f"qam_{recording}",
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
                query = parse_qs(urlparse(route.request.url).query)
                assert query["maximum_recordings"] == ["100"]
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "candidate_only": True,
                            "calibration_required": True,
                            "calibrated_detection_count": None,
                            "truncated": False,
                            "recordings": [
                                {
                                    "recording_id": "rec_ready_a",
                                    "state": "complete",
                                    "candidates": [
                                        _candidate(
                                            "rec_ready_a", "lnb_a1", "rx_a1", 0.8
                                        ),
                                        _candidate(
                                            "rec_ready_a", "lnb_a2", "rx_a2", 0.02
                                        ),
                                    ],
                                    "reason_codes": [],
                                },
                                {
                                    "recording_id": "rec_pending_a",
                                    "state": "pending",
                                    "candidates": [],
                                    "reason_codes": ["acquired-qam-analysis-pending"],
                                },
                            ],
                        }
                    ),
                )

            page.route("**/api/v22/capture-qam-summaries?*", fulfill)
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
            assert len(requests) == 1
        finally:
            browser.close()
