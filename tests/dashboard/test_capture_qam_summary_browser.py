from __future__ import annotations

import json

from playwright.sync_api import expect, sync_playwright

from tests.dashboard.test_doppler_visualization_browser import (
    browser_environment,
    running_dashboard,
)
from tests.e2e.test_dashboard_browser import _freeze_browser_clock


def _payload() -> dict[str, object]:
    def candidate(recording: str, radio: str, lnb: str, receiver: str, goodness: float):
        return {
            "recording_id": recording,
            "radio_id": radio,
            "lnb_id": lnb,
            "receiver_chain_id": receiver,
            "segment_id": f"seg_{recording}_{receiver}",
            "edge": "lower",
            "qam_goodness": goodness,
            "hard_symbol_accuracy": 0.80 if goodness > 0.5 else 0.26,
            "rms_evm": 0.78 if goodness > 0.5 else 4.0,
            "window_count": 32,
            "analysis_id": f"qam_{recording}",
        }

    return {
        "schema_version": 1,
        "start_utc_ns": 1,
        "stop_utc_ns": 2,
        "candidate_only": True,
        "calibration_required": True,
        "calibrated_detection_count": None,
        "recordings": [
            {
                "recording_id": "rec_ready_a",
                "radio_id": "radio_a",
                "analysis_state": "complete",
                "state": "complete",
                "candidates": [
                    candidate("rec_ready_a", "radio_a", "lnb_a1", "rx_a1", 0.82),
                    candidate("rec_ready_a", "radio_a", "lnb_a2", "rx_a2", 0.08),
                ],
                "reason_codes": [],
            },
            {
                "recording_id": "rec_pending_a",
                "radio_id": "radio_a",
                "analysis_state": "running",
                "state": "pending",
                "candidates": [],
                "reason_codes": ["acquired-qam-analysis-pending"],
            },
        ],
        "original_recording_count": 2,
        "truncated": False,
        "warnings": [
            "candidate-only-qam-goodness-not-starlink-detection",
            "highest-goodness-selected-independently-per-authoritative-lnb-receiver",
            "radio-lnb-receiver-series-are-never-pooled",
        ],
    }


def test_master_table_renders_one_bulk_qam_request_without_pooling_receivers() -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page()
            _freeze_browser_clock(page)
            requests: list[str] = []

            def fulfill(route):
                requests.append(route.request.url)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_payload()),
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
            expect(ready).to_contain_text("lnb_a1 / rx_a1: 0.820 · high")
            expect(ready).to_contain_text("lnb_a2 / rx_a2: 0.080 · low")
            expect(pending).to_have_attribute("data-state", "pending")
            expect(pending).to_have_text("Pending")
            assert len(requests) == 1
        finally:
            browser.close()
