from __future__ import annotations

import json

import pytest
from playwright.sync_api import Route, expect, sync_playwright

from tests.dashboard.test_doppler_visualization_browser import (
    browser_environment,
    running_dashboard,
)

RECORDING_ID = "rec_temporal_browser"


def _winner(score: float, epoch: int, cfo: float) -> dict[str, object]:
    return {
        "score": score,
        "winning_epoch_sample": epoch,
        "winning_coarse_cfo_hz": cfo,
        "winning_residual_cfo_hz": 50.0,
        "effective_search_cell_count": 18_656,
        "search_mode": "searched",
    }


def _payload() -> dict[str, object]:
    points = []
    for index, start in enumerate((0, 12_500_000, 49_980_000)):
        scores = (0.2 + index * 0.2, 0.15 + index * 0.1, 0.1, 0.3)
        qin = 0.25 + index * 0.25
        points.append(
            {
                "probe_index": index,
                "start_sample": start,
                "stop_sample": start + 20_000,
                "center_sample": start + 10_000.0,
                "interval_start_utc_ns": 1_800_000_000_000_000_000 + start * 400,
                "interval_stop_utc_ns": 1_800_000_000_000_000_000
                + (start + 20_000) * 400,
                "method": "glrt-32",
                "qin": _winner(qin, 64, 20_000.0),
                "surrogates": [
                    {
                        "codebook_index": control,
                        "template_digest": {
                            "algorithm": "sha256",
                            "value": f"{control + 1:064x}",
                        },
                        "winner": _winner(score, 128, -20_000.0),
                    }
                    for control, score in enumerate(scores)
                ],
                "finite_upper_tail_rank": 1 + sum(score >= qin for score in scores),
                "qin_minus_max_surrogate": qin - max(scores),
            }
        )
    return {
        "schema": {
            "schema_id": "org.leo-flow.dashboard.recording-starlink-temporal-pilot",
            "version": {"major": 0, "minor": 1},
        },
        "recording_id": RECORDING_ID,
        "analysis_ref": {},
        "plan": {
            "window_sample_count": 20_000,
            "nominal_stride_samples": 12_500_000,
            "maximum_probe_count": 8,
            "surrogate_count": 4,
        },
        "streams": [
            {
                "radio_id": "radio_20",
                "segment_id": "seg_ch4_lower",
                "receiver_chain_id": "rx_0",
                "channel_number": 4,
                "edge": "lower",
                "sample_rate_hz": 2_500_000.0,
                "segment_sample_count": 50_000_000,
                "analyzed_sample_count": 60_000,
                "coverage_fraction": 0.0012,
                "points": points,
                "dwell_summaries": [
                    {
                        "method": "glrt-32",
                        "qin_maximum": 0.75,
                        "surrogate_maxima": [0.6, 0.35, 0.1, 0.3],
                        "finite_upper_tail_rank": 1,
                        "qin_minus_max_surrogate": 0.15,
                        "candidate_window_count": 2,
                        "probe_count": 3,
                    }
                ],
            }
        ],
        "original_point_count": 3,
        "truncated": False,
        "decimation": "none",
        "warnings": [
            "candidate-evidence-not-calibrated-detection",
            "finite-surrogate-rank-not-p-value",
            "dwell-maxima-include-time-look-elsewhere",
            "overlapping-windows-statistically-dependent",
        ],
    }


def test_browser_renders_temporal_trace_coverage_filters_and_exact_tooltip() -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1600})
            urls: list[str] = []

            def fulfill(route: Route) -> None:
                urls.append(route.request.url)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_payload()),
                )

            page.route(
                f"**/api/v13/recordings/{RECORDING_ID}/starlink-temporal-pilot?*",
                fulfill,
            )
            response = page.goto(f"{base_url}/recordings/{RECORDING_ID}")
            assert response is not None and response.ok
            page.locator("#evidence-load-extended").click()
            expect(page.locator("#temporal-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#temporal-warning")).to_contain_text(
                "not continuous coverage"
            )
            expect(page.locator("#temporal-facts")).to_contain_text("0.1200%")
            expect(page.locator("#temporal-facts")).to_contain_text(
                "Stream (never pooled)"
            )
            canvas = page.locator("#temporal-chart")
            canvas.focus()
            canvas.press("ArrowRight")
            expect(page.locator("#temporal-tooltip")).to_contain_text("Window 2")
            expect(page.locator("#temporal-tooltip")).to_contain_text(
                "coarse CFO 20000.000 Hz"
            )
            page.locator("#temporal-radio").select_option("radio_20")
            page.locator("#temporal-receiver").select_option("rx_0")
            page.locator("#temporal-edge").select_option("lower")
            expect(page.locator("#temporal-state")).to_have_attribute(
                "data-state", "ready"
            )
            assert any("radio_ids=radio_20" in url for url in urls)
            assert any("receiver_chain_ids=rx_0" in url for url in urls)
            assert any("edges=lower" in url for url in urls)
        finally:
            browser.close()


@pytest.mark.parametrize("status,state", [(404, "unavailable"), (500, "error")])
def test_browser_temporal_missing_and_error_states(status: int, state: str) -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page()
            page.route(
                f"**/api/v13/recordings/{RECORDING_ID}/starlink-temporal-pilot?*",
                lambda route: route.fulfill(
                    status=status,
                    content_type="application/json",
                    body=json.dumps({"error": {"message": "temporal unavailable"}}),
                ),
            )
            page.goto(f"{base_url}/recordings/{RECORDING_ID}")
            page.locator("#evidence-load-extended").click()
            expect(page.locator("#temporal-state")).to_have_attribute(
                "data-state", state
            )
        finally:
            browser.close()


def test_browser_temporal_stays_pending_until_product_arrives() -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        held: list[Route] = []
        try:
            page = browser.new_page()
            page.route(
                f"**/api/v13/recordings/{RECORDING_ID}/starlink-temporal-pilot?*",
                lambda route: held.append(route),
            )
            page.goto(f"{base_url}/recordings/{RECORDING_ID}")
            page.locator("#evidence-load-extended").click()
            expect(page.locator("#temporal-state")).to_have_attribute(
                "data-state", "pending"
            )
            expect(page.locator("#temporal-summary")).to_be_hidden()
            assert len(held) == 1
            held[0].fulfill(
                status=200, content_type="application/json", body=json.dumps(_payload())
            )
            expect(page.locator("#temporal-state")).to_have_attribute(
                "data-state", "ready"
            )
        finally:
            browser.close()


def test_aggregate_page_renders_per_probe_maxima_and_exact_coverage() -> None:
    density = {
        "schema_version": 1,
        "start_utc_ns": 1_786_999_786_798_682_805,
        "stop_utc_ns": 1_800_000_000_000_000_000,
        "bin_count": 40,
        "point_identity": "recording+segment+radio+receiver-chain+edge+method+pattern",
        "recording_count": 1,
        "truncated": False,
        "distributions": [],
        "warnings": [
            "finite-surrogate-ensemble-not-calibrated-null-distribution",
            "candidate-evidence-not-detection",
        ],
    }
    temporal = {
        "schema_version": 1,
        "start_utc_ns": density["start_utc_ns"],
        "stop_utc_ns": density["stop_utc_ns"],
        "recording_count": 1,
        "truncated": False,
        "strata": [
            {
                "method": "glrt-32",
                "radio_id": "radio_20",
                "receiver_chain_id": "rx_0",
                "edge": "lower",
                "recording_count": 1,
                "probe_count": 5,
                "mean_probe_maximum_qin_score": 0.41,
                "mean_probe_maximum_surrogate_score": 0.35,
                "mean_union_coverage_fraction": 0.002,
                "candidate_window_fraction": 0.4,
            }
        ],
        "warnings": [
            "stratified-sampling-not-continuous-coverage",
            "candidate-evidence-not-calibrated-detection",
        ],
    }
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page()
            page.route(
                "**/api/v12/surrogate-score-distributions?*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(density),
                ),
            )
            page.route(
                "**/api/v13/temporal-pilot-aggregate?*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(temporal),
                ),
            )
            page.goto(f"{base_url}/aggregate-stats")
            expect(page.locator("#temporal-aggregate-state")).to_have_attribute(
                "data-state", "ready"
            )
            row = page.locator("#temporal-summary-body tr")
            expect(row).to_have_count(1)
            expect(row).to_contain_text("glrt-32 · radio_20 · rx_0 · lower")
            expect(row).to_contain_text("0.410000")
            expect(row).to_contain_text("0.2000%")
            expect(row).to_contain_text("40.00% · candidate only")
        finally:
            browser.close()
