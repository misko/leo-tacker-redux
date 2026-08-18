from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Route, expect, sync_playwright

from tests.dashboard.test_doppler_visualization_browser import (
    browser_environment,
    running_dashboard,
)

RECORDING = "rec_evidence_ui"


def _context() -> dict[str, object]:
    return {
        "requested_recording_id": RECORDING,
        "capture_batch_id": "cbatch_ui",
        "recordings": [
            {
                "recording_id": RECORDING,
                "radio_id": "radio_a",
                "radio_serial": "serial-a",
                "analysis_state": "complete",
                "requested": True,
            }
        ],
        "receivers": [
            {
                "recording_id": RECORDING,
                "radio_id": "radio_a",
                "receiver_chain_id": "rx_a",
                "lnb_id": "lnb_authoritative",
                "radio_channel": 0,
            }
        ],
        "segments": [
            {
                "recording_id": RECORDING,
                "segment_id": "seg_a",
                "receiver_chain_ids": ["rx_a"],
            }
        ],
        "candidate_only": True,
        "calibrated_detection_count": None,
        "warnings": ["candidate-only-evidence-not-calibrated-detection"],
        "limitations": [],
    }


def _qam(mode: str) -> dict[str, object]:
    point = {"i": 0.7, "q": 0.72, "expected_state": 0}
    windows = [
        {
            "window_index": index,
            "start_sample": index * 100,
            "stop_sample": index * 100 + 100,
            "interval_start_utc_ns": 1_000_000_000 + index * 100_000_000,
            "interval_stop_utc_ns": 1_100_000_000 + index * 100_000_000,
            "hard_symbol_accuracy": 0.7483333333333333 if index == 0 else 0.25,
            "rms_evm": 0.942548533 if index == 0 else 18.0,
            "display_points": [point],
        }
        for index in range(2 if mode == "windows" else 1)
    ]
    return {
        "recording_id": RECORDING,
        "mode": mode,
        "candidate_only": True,
        "calibration_required": True,
        "streams": [
            {
                "radio_id": "radio_a",
                "lnb_id": "lnb_authoritative",
                "segment_id": "seg_a",
                "receiver_chain_id": "rx_a",
                "edge": "lower",
                "overall": {
                    "selected_display_window_index": 0,
                    "support_weighted_hard_symbol_accuracy": 0.25,
                    "support_weighted_rms_evm": 18.0,
                },
                "windows": windows,
            }
        ],
    }


def _full_dwell() -> dict[str, object]:
    winner = {"score": 0.8}
    return {
        "recording_id": RECORDING,
        "queue_state": "complete",
        "streams": [
            {
                "radio_id": "radio_a",
                "segment_id": "seg_a",
                "receiver_chain_id": "rx_a",
                "channel_number": 4,
                "edge": "lower",
                "points": [
                    {
                        "method": "glrt-32",
                        "interval_start_utc_ns": 1_000_000_000,
                        "interval_stop_utc_ns": 1_100_000_000,
                        "qin": winner,
                        "surrogates": [{"winner": {"score": 0.2}}],
                    }
                ],
            }
        ],
    }


def _adaptive_response() -> dict[str, object]:
    payload = _full_dwell()
    stream = dict(payload["streams"][0])  # type: ignore[index]
    stream.update(
        {
            "lnb_id": "lnb_authoritative",
            "sample_rate_hz": 2_500_000.0,
            "selection": {
                "exact_windows": [
                    {
                        "start_sample": 0,
                        "stop_sample": 20_000,
                        "stage": "sentinel",
                    }
                ]
            },
            "exact_coverage_fraction": 1.0,
        }
    )
    return {
        "recording_id": RECORDING,
        "candidate_only": True,
        "calibration_required": True,
        "plan": {"probe_sample_count": 20_000},
        "streams": [stream],
    }


def _prompt_timeline() -> dict[str, object]:
    return {
        "recording_id": RECORDING,
        "candidate_only": True,
        "calibrated_detection_count": None,
        "prescreen_window_samples": 20_000,
        "original_window_count": 2,
        "returned_window_count": 2,
        "truncated": False,
        "streams": [
            {
                "radio_id": "radio_a",
                "segment_id": "seg_a",
                "receiver_chain_id": "rx_a",
                "channel_number": 4,
                "edge": "lower",
                "sample_rate_hz": 2_500_000.0,
                "windows": [
                    {
                        "interval_start_utc_ns": 1_000_000_000,
                        "interval_stop_utc_ns": 1_008_000_000,
                        "mean_complex_power": 12.0,
                        "selected_for_exact_refinement": True,
                    },
                    {
                        "interval_start_utc_ns": 1_008_000_000,
                        "interval_stop_utc_ns": 1_016_000_000,
                        "mean_complex_power": 8.0,
                        "selected_for_exact_refinement": False,
                    },
                ],
            }
        ],
    }


def _doppler() -> dict[str, object]:
    return {
        "requested_recording_id": RECORDING,
        "state": "complete",
        "candidate_only": True,
        "calibrated_detection_count": None,
        "series": [
            {
                "recording_id": RECORDING,
                "radio_id": "radio_a",
                "lnb_id": "lnb_authoritative",
                "receiver_chain_id": "rx_a",
                "segment_id": "seg_a",
                "candidate_rank": 1,
                "total": {"drift_rate_hz_s": 25000.0},
                "windows": [
                    {
                        "interval_start_utc_ns": 1_000_000_000,
                        "interval_stop_utc_ns": 1_100_000_000,
                        "start_sample": 0,
                        "stop_sample": 100,
                        "drift_rate_hz_s": 24000.0,
                        "support_count": 2,
                    },
                    {
                        "interval_start_utc_ns": 1_100_000_000,
                        "interval_stop_utc_ns": 1_200_000_000,
                        "start_sample": 100,
                        "stop_sample": 200,
                        "drift_rate_hz_s": 26000.0,
                        "support_count": 2,
                    },
                ],
            }
        ],
        "original_window_count": 2,
        "truncated": False,
    }


def _advanced_doppler() -> dict[str, object]:
    return {
        "requested_recording_id": RECORDING,
        "state": "complete",
        "candidate_only": True,
        "calibrated_detection_count": None,
        "series": [
            {
                "recording_id": RECORDING,
                "radio_id": "radio_a",
                "lnb_id": "lnb_authoritative",
                "receiver_chain_id": "rx_a",
                "segment_id": "seg_a",
                "association_state": "advanced-path-only",
                "total": {"drift_rate_hz_s": -12500.0},
                "windows": [
                    {
                        "point_start_utc_ns": 1_000_000_000,
                        "point_stop_utc_ns": 1_100_000_000,
                        "interval_start_utc_ns": 950_000_000,
                        "interval_stop_utc_ns": 1_150_000_000,
                        "start_sample": 0,
                        "stop_sample": 200,
                        "drift_rate_hz_s": -12000.0,
                        "support_count": 2,
                    },
                    {
                        "point_start_utc_ns": 1_100_000_000,
                        "point_stop_utc_ns": 1_200_000_000,
                        "interval_start_utc_ns": 1_050_000_000,
                        "interval_stop_utc_ns": 1_250_000_000,
                        "start_sample": 100,
                        "stop_sample": 300,
                        "drift_rate_hz_s": -13000.0,
                        "support_count": 2,
                    },
                ],
            }
        ],
        "original_window_count": 2,
        "truncated": False,
    }


def test_unified_workspace_switches_real_overall_windows_and_never_pools() -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1500, "height": 1200})
            qam_modes: list[str] = []
            page.route(
                f"**/api/v16/recordings/{RECORDING}/evidence-context",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_context()),
                ),
            )

            def qam(route: Route) -> None:
                mode = parse_qs(urlparse(route.request.url).query)["mode"][0]
                qam_modes.append(mode)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_qam(mode)),
                )

            page.route(
                f"**/api/v17/recordings/{RECORDING}/starlink-acquired-constellation?*",
                qam,
            )
            page.route(
                f"**/api/v15/recordings/{RECORDING}/starlink-full-dwell?*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_full_dwell()),
                ),
            )
            page.route(
                f"**/api/v24/recordings/{RECORDING}/starlink-adaptive-response?*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_adaptive_response()),
                ),
            )
            page.route(
                f"**/api/v20/recordings/{RECORDING}/full-dwell-timeline?*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_prompt_timeline()),
                ),
            )
            page.route(
                f"**/api/v16/recordings/{RECORDING}/evidence-doppler?*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_doppler()),
                ),
            )
            page.route(
                f"**/api/v19/recordings/{RECORDING}/evidence-advanced-doppler?*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_advanced_doppler()),
                ),
            )
            page.goto(f"{base_url}/recordings/{RECORDING}")
            expect(page.locator("#evidence-context-state")).to_have_attribute(
                "data-state", "ready"
            )
            for product in ("timeline", "qam", "detector", "doppler"):
                expect(page.locator(f"#evidence-{product}-state")).to_have_attribute(
                    "data-state", "ready"
                )
                expect(page.locator(f"#evidence-{product}-canvas")).to_be_visible()
            expect(page.locator("#evidence-qam-canvas")).to_have_attribute(
                "data-series-count", "1"
            )
            expect(page.locator("#evidence-timeline-canvas")).to_have_attribute(
                "data-point-count", "3"
            )
            expect(page.locator("#evidence-timeline-state")).to_contain_text(
                "source union coverage is 100%"
            )
            goodness = page.locator("#evidence-qam-goodness .qam-goodness-entry")
            expect(goodness).to_have_count(1)
            expect(goodness).to_have_attribute("data-goodness", "0.737354")
            expect(goodness).to_have_attribute("data-goodness-band", "high")
            expect(page.locator("#evidence-detector-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            expect(page.locator("#evidence-doppler-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            expect(page.locator("#evidence-doppler-legend")).to_contain_text(
                "advanced path only"
            )
            page.locator("#evidence-mode").select_option("windows")
            expect(page.locator("#evidence-qam-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            window_goodness = page.locator("#evidence-qam-goodness .qam-goodness-entry")
            expect(window_goodness).to_have_count(2)
            expect(window_goodness.nth(0)).to_have_attribute(
                "data-goodness", "0.737354"
            )
            expect(window_goodness.nth(0)).to_have_attribute(
                "data-goodness-band", "high"
            )
            expect(window_goodness.nth(1)).to_have_attribute(
                "data-goodness", "0.000000"
            )
            expect(window_goodness.nth(1)).to_have_attribute(
                "data-goodness-band", "low"
            )
            expect(page.locator("#evidence-doppler-canvas")).to_have_attribute(
                "data-point-count", "4"
            )
            assert "overall" in qam_modes and "windows" in qam_modes
            page.locator('#evidence-lnbs input[value="lnb_authoritative"]').uncheck()
            for product in ("qam", "detector", "doppler"):
                expect(page.locator(f"#evidence-{product}-state")).to_have_attribute(
                    "data-state", "missing"
                )
                expect(page.locator(f"#evidence-{product}-canvas")).to_be_hidden()
            page.locator('#evidence-lnbs input[value="lnb_authoritative"]').check()
            expect(page.locator("#evidence-detector-state")).to_have_attribute(
                "data-state", "ready"
            )
            page.locator('#evidence-methods input[value="glrt-32"]').uncheck()
            expect(page.locator("#evidence-detector-state")).to_have_attribute(
                "data-state", "missing"
            )
            page.locator('#evidence-methods input[value="glrt-32"]').check()
            page.locator('#evidence-patterns input[value="surrogate-0"]').uncheck()
            expect(page.locator("#evidence-detector-canvas")).to_have_attribute(
                "data-series-count", "1"
            )
            page.locator('#evidence-patterns input[value="surrogate-0"]').check()
            page.locator('#evidence-receivers input[value="rx_a"]').uncheck()
            for product in ("qam", "detector", "doppler"):
                expect(page.locator(f"#evidence-{product}-state")).to_have_attribute(
                    "data-state", "missing"
                )
            page.locator('#evidence-receivers input[value="rx_a"]').check()
            page.locator('#evidence-channels input[value="4"]').uncheck()
            expect(page.locator("#evidence-detector-state")).to_have_attribute(
                "data-state", "missing"
            )
            page.locator('#evidence-channels input[value="4"]').check()
            page.locator('#evidence-edges input[value="lower"]').uncheck()
            expect(page.locator("#evidence-qam-state")).to_have_attribute(
                "data-state", "missing"
            )
            expect(page.locator("#evidence-detector-state")).to_have_attribute(
                "data-state", "missing"
            )
            page.locator('#evidence-radios input[value="rec_evidence_ui"]').uncheck()
            for product in ("qam", "detector", "doppler"):
                expect(page.locator(f"#evidence-{product}-state")).to_have_attribute(
                    "data-state", "missing"
                )
        finally:
            browser.close()


def test_unified_workspace_distinguishes_pending_missing_and_error() -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page()
            page.route(
                f"**/api/v16/recordings/{RECORDING}/evidence-context",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_context()),
                ),
            )
            page.route(
                f"**/api/v17/recordings/{RECORDING}/starlink-acquired-constellation?*",
                lambda route: route.fulfill(
                    status=404, content_type="application/json", body="{}"
                ),
            )
            page.route(
                f"**/api/v15/recordings/{RECORDING}/starlink-full-dwell?*",
                lambda route: route.fulfill(
                    status=500,
                    content_type="application/json",
                    body='{"error":{"message":"failed"}}',
                ),
            )
            page.route(
                f"**/api/v24/recordings/{RECORDING}/starlink-adaptive-response?*",
                lambda route: route.fulfill(
                    status=500,
                    content_type="application/json",
                    body='{"error":{"message":"failed"}}',
                ),
            )
            timeline_pattern = (
                f"**/api/v20/recordings/{RECORDING}/full-dwell-timeline?*"
            )
            page.route(
                timeline_pattern,
                lambda route: route.fulfill(
                    status=500,
                    content_type="application/json",
                    body='{"error":{"message":"timeline failed"}}',
                ),
            )
            missing = {
                **_doppler(),
                "state": "missing",
                "series": [],
                "original_window_count": 0,
            }
            page.route(
                f"**/api/v16/recordings/{RECORDING}/evidence-doppler?*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(missing),
                ),
            )
            page.route(
                f"**/api/v19/recordings/{RECORDING}/evidence-advanced-doppler?*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({**missing, "warnings": []}),
                ),
            )
            page.goto(f"{base_url}/recordings/{RECORDING}")
            expect(page.locator("#evidence-qam-state")).to_have_attribute(
                "data-state", "pending"
            )
            expect(page.locator("#evidence-detector-state")).to_have_attribute(
                "data-state", "error"
            )
            expect(page.locator("#evidence-doppler-state")).to_have_attribute(
                "data-state", "missing"
            )
            expect(page.locator("#evidence-timeline-state")).to_have_attribute(
                "data-state", "error"
            )
            page.unroute(timeline_pattern)
            page.route(
                timeline_pattern,
                lambda route: route.fulfill(
                    status=404, content_type="application/json", body="{}"
                ),
            )
            page.reload()
            expect(page.locator("#evidence-timeline-state")).to_have_attribute(
                "data-state", "pending"
            )
        finally:
            browser.close()
