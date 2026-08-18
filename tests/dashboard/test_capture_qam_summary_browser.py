from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

from tests.dashboard.test_doppler_visualization_browser import (
    browser_environment,
    running_dashboard,
)


def _candidate(
    recording: str,
    lnb: str,
    receiver: str,
    accuracy: float,
    evm: float,
) -> dict[str, object]:
    return {
        "recording_id": recording,
        "radio_id": "radio_a",
        "lnb_id": lnb,
        "receiver_chain_id": receiver,
        "segment_id": f"seg_{receiver}",
        "edge": "lower",
        "qam_goodness": 0.75 if receiver == "rx_a1" else 0.01,
        "hard_symbol_accuracy": accuracy,
        "rms_evm": evm,
        "window_count": 32,
        "analysis_id": f"qam_{recording}",
        "selection": "highest-qam-goodness-per-recording-radio-lnb-receiver",
    }


def test_master_table_progressively_loads_qam_and_links_every_recording_detail() -> (
    None
):
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page()
            page.add_init_script(
                """
                (() => {
                  const NativeDate = Date;
                  class FixedDate extends NativeDate {
                    constructor(...args) { super(...(args.length ? args : [7200000])); }
                    static now() { return 7200000; }
                  }
                  FixedDate.parse = NativeDate.parse;
                  FixedDate.UTC = NativeDate.UTC;
                  globalThis.Date = FixedDate;
                })();
                """
            )
            requests: list[str] = []

            def fulfill(route):
                requests.append(route.request.url)
                query = parse_qs(urlparse(route.request.url).query)
                assert query == {
                    "start_utc_ns": ["0"],
                    "stop_utc_ns": ["7200000000000"],
                    "maximum_recordings": ["100"],
                }
                route.fulfill(
                    status=200,
                    json={
                        "schema_version": 1,
                        "start_utc_ns": 0,
                        "stop_utc_ns": 7_200_000_000_000,
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
                                    _candidate(
                                        "rec_ready_a", "lnb_a1", "rx_a1", 0.80, 0.78
                                    ),
                                    _candidate(
                                        "rec_ready_a", "lnb_a2", "rx_a2", 0.26, 4.0
                                    ),
                                ],
                                "reason_codes": [],
                            },
                            {
                                "recording_id": "rec_pending_a",
                                "radio_id": "radio_a",
                                "analysis_state": "pending",
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
                            "best-analyzed-window-not-support-weighted-dwell-mean",
                            "radio-lnb-receiver-series-are-never-pooled",
                        ],
                    },
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
