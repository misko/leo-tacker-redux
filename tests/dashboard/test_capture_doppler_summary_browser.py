from __future__ import annotations

import json

from playwright.sync_api import expect, sync_playwright

from tests.dashboard.test_doppler_visualization_browser import (
    browser_environment,
    running_dashboard,
)
from tests.e2e.test_dashboard_browser import _freeze_browser_clock


def _payload() -> dict[str, object]:
    def candidate(recording: str, radio: str, lnb: str, receiver: str, drift: float):
        return {
            "recording_id": recording,
            "radio_id": radio,
            "lnb_id": lnb,
            "receiver_chain_id": receiver,
            "segment_id": f"seg_{recording}_{receiver}",
            "candidate_id": f"candidate:{recording}:{receiver}:1",
            "model": "linear",
            "drift_rate_hz_s": drift,
            "ranking_score": 9.0,
            "doppler_id": f"doppler_{recording}_{receiver}",
            "algorithm_version": "0.1.0",
        }

    return {
        "schema_version": 1,
        "start_utc_ns": 1,
        "stop_utc_ns": 2,
        "candidate_only": True,
        "calibrated_detection_count": None,
        "recordings": [
            {
                "recording_id": "rec_ready_a",
                "radio_id": "radio_a",
                "analysis_state": "complete",
                "state": "complete",
                "candidates": [
                    candidate("rec_ready_a", "radio_a", "lnb_a1", "rx_a1", 24_000.0),
                    candidate("rec_ready_a", "radio_a", "lnb_a2", "rx_a2", 26_000.0),
                ],
                "reason_codes": [],
            },
            {
                "recording_id": "rec_ready_b",
                "radio_id": "radio_b",
                "analysis_state": "complete",
                "state": "complete",
                "candidates": [
                    candidate("rec_ready_b", "radio_b", "lnb_b1", "rx_b1", -12_000.0),
                    candidate("rec_ready_b", "radio_b", "lnb_b2", "rx_b2", -14_000.0),
                ],
                "reason_codes": [],
            },
            {
                "recording_id": "rec_pending_a",
                "radio_id": "radio_a",
                "analysis_state": "running",
                "state": "pending",
                "candidates": [],
                "reason_codes": ["doppler-analysis-pending"],
            },
        ],
        "original_recording_count": 3,
        "truncated": False,
        "warnings": [
            "candidate-only-evidence-not-satellite-detection",
            "highest-score-selected-independently-per-authoritative-lnb-receiver",
            "radio-lnb-receiver-candidates-are-never-pooled",
        ],
    }


def test_master_table_uses_one_bulk_request_and_keeps_two_radios_two_lnbs_unpooled() -> (
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
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_payload()),
                )

            page.route("**/api/v18/capture-doppler-summaries?*", fulfill)
            page.goto(base_url)
            ready_a = page.locator(
                '[data-recording-id="rec_ready_a"] .capture-doppler-summary'
            )
            ready_b = page.locator(
                '[data-recording-id="rec_ready_b"] .capture-doppler-summary'
            )
            pending = page.locator(
                '[data-recording-id="rec_pending_a"] .capture-doppler-summary'
            )
            expect(ready_a).to_have_attribute("data-state", "complete")
            expect(ready_a.locator(".capture-doppler-candidate")).to_have_count(2)
            expect(ready_a).to_contain_text("lnb_a1 / rx_a1: 24.00 kHz/s")
            expect(ready_a).to_contain_text("lnb_a2 / rx_a2: 26.00 kHz/s")
            expect(ready_b.locator(".capture-doppler-candidate")).to_have_count(2)
            expect(ready_b).to_contain_text("lnb_b1 / rx_b1: -12.00 kHz/s")
            expect(pending).to_have_attribute("data-state", "pending")
            expect(pending).to_have_text("Pending")
            expect(ready_a).to_have_attribute(
                "title",
                "Highest public ranking-score candidate selected independently for each authoritative LNB / receiver; not a calibrated detection",
            )
            assert len(requests) == 1
        finally:
            browser.close()


def test_master_table_bulk_failure_is_distinct_from_pending_and_unavailable() -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page()
            _freeze_browser_clock(page)
            page.route(
                "**/api/v18/capture-doppler-summaries?*",
                lambda route: route.fulfill(
                    status=500,
                    content_type="application/json",
                    body='{"error":{"message":"failed"}}',
                ),
            )
            page.goto(base_url)
            cell = page.locator(
                '[data-recording-id="rec_ready_a"] .capture-doppler-summary'
            )
            expect(cell).to_have_attribute("data-state", "error")
            expect(cell).to_have_text("Error")
        finally:
            browser.close()
