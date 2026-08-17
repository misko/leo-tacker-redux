from __future__ import annotations

import json

from playwright.sync_api import expect, sync_playwright

from tests.e2e.test_dashboard_browser import (
    _browser_environment,
    _running_dashboard,
)


def _payload(*, empty: bool = False) -> str:
    series = []
    summaries = []
    controls = []
    if not empty:
        for radio_index, radio in enumerate(("radio_a", "radio_b")):
            for receiver_index, receiver in enumerate(("rx_lnb_a", "rx_lnb_b")):
                source_index = radio_index * 2 + receiver_index
                common = {
                    "recording_id": f"rec_{source_index}",
                    "recording_started_utc_ns": 1_000_000_000,
                    "radio_id": radio,
                    "receiver_chain_id": receiver,
                    "segment_id": "seg_focus_ch4_lower",
                    "channel": "CH4",
                    "edge": "lower",
                    "doppler_id": "doppler_" + str(source_index + 1) * 32,
                    "waterfall_product_id": "waterfall_" + "9" * 32,
                    "candidate_or_path_id": f"path_{source_index}",
                    "method": "basic",
                    "algorithm_version": "blind-doppler-v0.1",
                    "model": "linear",
                    "association_state": "basic-candidate",
                    "reference_utc_ns": 1_500_000_000,
                    "reference_frequency_hz": 11_325_000_000.0,
                    "drift_rate_hz_s": float((source_index + 1) * 100),
                    "ranking_or_heldout_score": 2.0,
                    "input_identity_digest": "sha256:" + "a" * 64,
                    "config_digest": "sha256:" + "b" * 64,
                    "basic_bundle_digest": "sha256:" + "c" * 64,
                    "advanced_bundle_digest": "sha256:" + "d" * 64,
                    "overlapping_observations": True,
                    "points": [
                        {
                            "midpoint_utc_ns": 1_000_000_000,
                            "relative_time_s": -0.5,
                            "frequency_offset_hz": -50.0,
                        },
                        {
                            "midpoint_utc_ns": 2_000_000_000,
                            "relative_time_s": 0.5,
                            "frequency_offset_hz": 50.0,
                        },
                    ],
                }
                series.append(common)
                summaries.append(
                    {
                        "radio_id": radio,
                        "receiver_chain_id": receiver,
                        "method": "basic",
                        "model": "linear",
                        "association_state": "basic-candidate",
                        "series_count": 1,
                        "median_drift_rate_hz_s": common["drift_rate_hz_s"],
                        "p10_drift_rate_hz_s": common["drift_rate_hz_s"],
                        "p90_drift_rate_hz_s": common["drift_rate_hz_s"],
                    }
                )
                controls.append(
                    {
                        "recording_id": f"rec_{source_index}",
                        "radio_id": radio,
                        "receiver_chain_id": receiver,
                        "segment_id": "seg_focus_ch4_lower",
                        "candidate_path_id": f"path_{source_index}",
                        "control_class": "time-shuffle",
                        "score": 0.5,
                    }
                )
    return json.dumps(
        {
            "schema_version": 1,
            "start_utc_ns": 1,
            "stop_utc_ns": 2,
            "recording_count": 4,
            "tile_count": 4,
            "available_recording_count": 4 if series else 0,
            "truncated": False,
            "series": series,
            "controls": controls,
            "summaries": summaries,
            "warnings": [
                "advanced-path-bins-not-converted-to-physical-frequency",
                "candidate-only-evidence-not-satellite-detection",
                "overlapping-track-observations-are-not-independent",
                "radio-and-receiver-series-are-never-pooled",
            ],
        }
    )


def test_four_sources_remain_distinct_and_visibility_toggles_do_not_refetch() -> None:
    with _running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=_browser_environment())
        page = browser.new_page()
        requests = 0

        def route_api(route) -> None:
            nonlocal requests
            requests += 1
            route.fulfill(status=200, content_type="application/json", body=_payload())

        page.route("**/api/v14/doppler-aggregate?*", route_api)
        response = page.goto(base_url + "/aggregate-doppler")
        assert response is not None and response.ok
        expect(page.locator("#doppler-aggregate-state")).to_have_attribute(
            "data-state", "complete"
        )
        expect(page.locator("#series-legend p")).to_have_count(4)
        for label in (
            "radio_a / rx_lnb_a",
            "radio_a / rx_lnb_b",
            "radio_b / rx_lnb_a",
            "radio_b / rx_lnb_b",
        ):
            expect(page.locator("#series-legend")).to_contain_text(label)

        page.locator('input[data-category="radio"][data-option="radio_a"]').uncheck()
        expect(page.locator("#series-legend p")).to_have_count(2)
        expect(page.locator("#series-legend")).not_to_contain_text("radio_a")
        assert requests == 1

        page.locator(
            'input[data-category="source"][data-option="radio_b / rx_lnb_a"]'
        ).uncheck()
        expect(page.locator("#series-legend p")).to_have_count(1)
        expect(page.locator("#series-legend")).to_contain_text("radio_b / rx_lnb_b")
        assert requests == 1

        page.locator("#load-doppler").click()
        expect(page.locator("#doppler-aggregate-state")).to_have_attribute(
            "data-state", "complete"
        )
        expect(
            page.locator('input[data-category="radio"][data-option="radio_a"]')
        ).not_to_be_checked()
        expect(page.locator("#series-legend p")).to_have_count(1)
        assert requests == 2
        browser.close()


def test_pending_and_error_states_are_explicit() -> None:
    with _running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=_browser_environment())
        pending = browser.new_page()
        pending.route(
            "**/api/v14/doppler-aggregate?*",
            lambda route: route.fulfill(
                status=200, content_type="application/json", body=_payload(empty=True)
            ),
        )
        pending.goto(base_url + "/aggregate-doppler")
        expect(pending.locator("#doppler-aggregate-state")).to_have_attribute(
            "data-state", "pending"
        )

        failed = browser.new_page()
        failed.route(
            "**/api/v14/doppler-aggregate?*",
            lambda route: route.fulfill(
                status=500, content_type="application/json", body='{"error":"failed"}'
            ),
        )
        failed.goto(base_url + "/aggregate-doppler")
        expect(failed.locator("#doppler-aggregate-state")).to_have_attribute(
            "data-state", "error"
        )
        expect(failed.locator("#doppler-aggregate-state")).to_contain_text("HTTP 500")
        browser.close()
