from __future__ import annotations

import json

from playwright.sync_api import Route, expect, sync_playwright

from tests.dashboard.test_doppler_visualization_browser import (
    browser_environment,
    running_dashboard,
)


def _payload() -> dict[str, object]:
    winner = {
        "score": 0.8,
        "winning_epoch_sample_in_window": 12,
        "winning_epoch_sample_in_segment": 1012,
        "winning_coarse_cfo_hz": 20000.0,
        "winning_residual_cfo_hz": 25.0,
        "effective_search_cell_count": 128,
        "search_mode": "searched",
        "aggregation": "maximum-over-declared-epoch-cfo-cells",
    }
    point = {
        "recording_id": "rec_fd_browser",
        "radio_id": "radio_2",
        "segment_id": "seg_4",
        "receiver_chain_id": "rx_1",
        "edge": "lower",
        "method": "glrt-32",
        "window_index": 0,
        "tier": "exact-refinement",
        "start_sample": 1000,
        "stop_sample": 21000,
        "interval_start_utc_ns": 1800000000000000000,
        "interval_stop_utc_ns": 1800000000008000000,
        "prescreen_score": 2.1,
        "qin": winner,
        "surrogates": [
            {
                "codebook_index": 0,
                "template_digest": {"algorithm": "sha256", "value": "1" * 64},
                "winner": {**winner, "score": 0.2},
            }
        ],
        "finite_upper_tail_rank": 1,
        "qin_minus_max_surrogate": 0.6,
        "dependence_group": "stream-a",
    }
    return {
        "recording_id": "rec_fd_browser",
        "analysis_ref": {},
        "plan": {"maximum_fine_window_count": 32},
        "streams": [
            {
                "radio_id": "radio_2",
                "segment_id": "seg_4",
                "receiver_chain_id": "rx_1",
                "channel_number": 4,
                "edge": "lower",
                "sample_rate_hz": 2500000.0,
                "segment_sample_count": 50000000,
                "prescreen_window_count": 2500,
                "exact_window_count": 32,
                "prescreen_coverage_fraction": 1.0,
                "exact_coverage_fraction": 0.0128,
                "refinement_is_data_adaptive": True,
                "points": [point],
            }
        ],
        "original_point_count": 1,
        "truncated": False,
        "decimation": "none",
        "queue_state": "complete",
        "backlog_depth": 0,
        "warnings": [],
    }


def test_browser_labels_sparse_exact_coverage_and_independent_filters() -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            urls: list[str] = []

            def fulfill(route: Route) -> None:
                urls.append(route.request.url)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_payload()),
                )

            page.route(
                "**/api/v15/recordings/rec_fd_browser/starlink-full-dwell?*", fulfill
            )
            response = page.goto(f"{base_url}/full-dwell")
            assert response is not None and response.ok
            expect(page.locator("#fd-warning")).to_contain_text(
                "pattern-blind power-prescreen coverage"
            )
            expect(page.locator("#fd-warning")).to_contain_text(
                "never full detector coverage"
            )
            page.locator("#fd-recording").fill("rec_fd_browser")
            page.locator("#full-dwell-controls").evaluate(
                "form => form.requestSubmit()"
            )
            expect(page.locator("#fd-state")).to_have_attribute("data-state", "ready")
            expect(page.locator("#fd-facts")).to_contain_text("1.280% maximum shown")
            expect(page.locator("#fd-radio option[value='radio_2']")).to_have_count(1)
            expect(page.locator("#fd-receiver option[value='rx_1']")).to_have_count(1)
            assert urls and "maximum_points=4096" in urls[0]
            assert page.locator("#fd-method option").evaluate_all(
                "options => options.slice(1).map(option => option.value)"
            ) == [
                "anchor-8",
                "differential-16",
                "differential-32",
                "glrt-32",
                "glrt-64",
                "full-frame-acquire",
                "full-frame-verify",
                "full-frame-full",
            ]
        finally:
            browser.close()


def test_browser_exposes_pending_and_error_states() -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page()
            page.route(
                "**/api/v15/recordings/rec_pending/starlink-full-dwell?*",
                lambda r: r.fulfill(status=404, body="{}"),
            )
            page.goto(f"{base_url}/full-dwell")
            page.locator("#fd-recording").fill("rec_pending")
            page.locator("#full-dwell-controls").evaluate(
                "form => form.requestSubmit()"
            )
            expect(page.locator("#fd-state")).to_have_attribute("data-state", "pending")
            expect(page.locator("#fd-state")).to_contain_text(
                "bounded asynchronous queue"
            )
        finally:
            browser.close()
