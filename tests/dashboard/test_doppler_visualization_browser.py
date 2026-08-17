from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Route, expect, sync_playwright

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.contracts.core import ReceiverChainId, SegmentId, canonical_json_bytes
from leo_flow.contracts.dashboard_doppler import (
    DopplerVisualizationState,
    DopplerWaterfallLayer,
)
from leo_flow.dashboard.api import DashboardJsonApplicationV2
from leo_flow.dashboard.ui import DashboardUiApplication
from tests.dashboard._fixtures import repository
from tests.dashboard.test_doppler_visualization_api import RECORDING_ID, visualization


def multi_tile_visualization(layer: DopplerWaterfallLayer):
    payload = visualization(layer)
    second_segment = SegmentId("seg_doppler_b")
    second_receiver = ReceiverChainId("rx_doppler_b")
    second_tile = replace(
        payload.tiles[0],
        segment_id=second_segment,
        receiver_chain_id=second_receiver,
        center_frequency_hz=10_756_000_000.0,
    )
    second_candidate = replace(
        payload.candidates[0],
        segment_id=second_segment,
        receiver_chain_id=second_receiver,
        drift_rate_hz_s=-12_000.0,
        mean_spectral_peak_excess_db=8.25,
        reference_frequency_hz=10_756_000_000.0,
        points=tuple(
            replace(point, frequency_hz=point.frequency_hz + 1_000_000.0)
            for point in payload.candidates[0].points
        ),
    )
    second_advanced = replace(
        payload.advanced_evidence[0],
        segment_id=second_segment,
        receiver_chain_id=second_receiver,
        drift_rate_hz_s=-12_000.0,
        orbit_association=None,
    )
    first_provenance = payload.doppler_provenance[0]
    assert first_provenance.advanced is not None
    second_provenance = replace(
        first_provenance,
        segment_id=second_segment,
        receiver_chain_id=second_receiver,
        basic=replace(first_provenance.basic, artifact_id="second_basic_product"),
        advanced=replace(
            first_provenance.advanced,
            artifact_id="advanced_doppler_bundle_b",
        ),
    )
    return replace(
        payload,
        tiles=(*payload.tiles, second_tile),
        candidates=(*payload.candidates, second_candidate),
        advanced_evidence=(*payload.advanced_evidence, second_advanced),
        doppler_provenance=(*payload.doppler_provenance, second_provenance),
    )


@contextmanager
def running_dashboard() -> Iterator[str]:
    server = StdlibDashboardServer(request_timeout_s=0.01)
    server.preflight("127.0.0.1", 0)
    queries = repository()
    application = DashboardUiApplication(DashboardJsonApplicationV2(queries, queries))
    stopped = threading.Event()

    def serve() -> None:
        while not stopped.is_set():
            server.serve_once(application)

    worker = threading.Thread(target=serve, name="doppler-dashboard-browser-server")
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.bound_port}"
    finally:
        stopped.set()
        worker.join(timeout=1)
        server.close(1)
        assert not worker.is_alive()


def browser_environment() -> dict[str, str | float | bool]:
    environment: dict[str, str | float | bool] = dict(os.environ)
    root = Path.home() / ".cache" / "ms-playwright" / "ubuntu-libs"
    if root.is_dir():
        environment["LD_LIBRARY_PATH"] = str(root / "usr/lib/x86_64-linux-gnu")
        environment["FONTCONFIG_FILE"] = str(root / "etc/fonts/fonts.conf")
        environment["FONTCONFIG_SYSROOT"] = str(root)
    return environment


def test_browser_selects_one_layer_and_renders_tracks_controls_and_provenance() -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1200})
            requested_layers: list[str] = []

            def fulfill_doppler(route: Route) -> None:
                layer_value = parse_qs(urlparse(route.request.url).query)["layer"][0]
                requested_layers.append(layer_value)
                layer = DopplerWaterfallLayer(layer_value)
                payload = multi_tile_visualization(layer)
                if layer is DopplerWaterfallLayer.AVERAGE:
                    advanced = tuple(
                        replace(item, orbit_association=None)
                        for item in payload.advanced_evidence
                    )
                    payload = replace(payload, advanced_evidence=advanced)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(json.loads(canonical_json_bytes(payload))),
                )

            page.route(
                f"**/api/v9/recordings/{RECORDING_ID}/doppler-visualization?*",
                fulfill_doppler,
            )
            response = page.goto(f"{base_url}/recordings/{RECORDING_ID}")
            assert response is not None and response.ok

            expect(page.locator("#doppler-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#doppler-warning")).to_be_visible()
            expect(page.locator("#doppler-warning")).to_contain_text(
                "Candidate-only evidence"
            )
            expect(page.locator("#doppler-figure")).to_be_visible()
            expect(page.locator("#doppler-canvas")).to_have_attribute(
                "aria-label",
                "Temporal-median residual Doppler waterfall for seg_doppler, "
                "receiver rx_doppler; 2 time bins by 3 frequency bins; "
                "1 candidate track overlays",
            )
            expect(page.locator("#doppler-time-axis")).to_contain_text("Time")
            expect(page.locator("#doppler-frequency-axis")).to_contain_text("Frequency")
            expect(page.locator("#doppler-coverage-facts")).to_contain_text("100.000%")
            expect(page.locator("#doppler-coverage-facts")).to_contain_text(
                "seg_doppler / rx_doppler"
            )
            expect(page.locator("#doppler-candidate-count")).to_have_text(
                "1 selected · 2 total"
            )
            candidate = page.locator('.doppler-candidate-card[data-rank="1"]')
            expect(candidate).to_contain_text("25000.000 Hz/s")
            expect(candidate).to_contain_text("0.000 Hz/s²")
            expect(candidate).to_contain_text("13.50 dB")
            expect(candidate).to_contain_text("Mean spectral peak excess")
            expect(candidate).not_to_contain_text("SNR")
            expect(candidate).to_contain_text("1.000 s")
            evidence = page.locator("#doppler-advanced-list")
            expect(evidence).to_contain_text("25000.000 Hz/s")
            expect(evidence).to_contain_text("local-temporal-median-db")
            expect(evidence).to_contain_text("Candidate 1")
            expect(evidence).to_contain_text("125.0 Hz / 250.0 Hz")
            expect(evidence).to_contain_text("Held-out score")
            expect(evidence).to_contain_text("Stationary control")
            expect(evidence).to_contain_text("Opposite-slope control")
            expect(evidence).to_contain_text("Time-shuffled controls")
            expect(evidence).to_contain_text("Comb fit / held-out")
            expect(evidence).to_contain_text("Post-blind TLE association")
            expect(evidence).to_contain_text("STARLINK-TEST")
            expect(page.locator("#doppler-advanced")).to_contain_text(
                "not an independently sampled null distribution"
            )

            page.locator("#doppler-provenance").click()
            expect(page.locator("#doppler-provenance-facts")).to_contain_text(
                "waterfall_doppler"
            )
            expect(page.locator("#doppler-provenance-facts")).to_contain_text(
                "blind_doppler_bundle"
            )
            expect(page.locator("#doppler-provenance-facts")).not_to_contain_text(
                "second_basic_product"
            )

            page.locator("#doppler-tile").select_option("1")
            expect(page.locator("#doppler-canvas")).to_have_attribute(
                "aria-label",
                "Temporal-median residual Doppler waterfall for seg_doppler_b, "
                "receiver rx_doppler_b; 2 time bins by 3 frequency bins; "
                "1 candidate track overlays",
            )
            expect(candidate).to_contain_text("-12000.000 Hz/s")
            expect(candidate).to_contain_text("8.25 dB")
            expect(evidence).to_contain_text("-12000.000 Hz/s")
            expect(page.locator("#doppler-provenance-facts")).to_contain_text(
                "second_basic_product"
            )
            expect(page.locator("#doppler-provenance-facts")).not_to_contain_text(
                "blind_doppler_bundle"
            )

            page.locator("#doppler-layer").select_option("average")
            expect(page.locator("#doppler-canvas")).to_have_attribute(
                "aria-label",
                "Average power Doppler waterfall for seg_doppler_b, receiver "
                "rx_doppler_b; 2 time bins by 3 frequency bins; "
                "1 candidate track overlays",
            )
            expect(evidence).not_to_contain_text("Post-blind TLE association")

            page.locator("#doppler-layer").select_option("high-percentile")
            expect(page.locator("#doppler-canvas")).to_have_attribute(
                "aria-label",
                "High percentile (P99.0) Doppler waterfall for seg_doppler_b, "
                "receiver rx_doppler_b; 2 time bins by 3 frequency bins; "
                "1 candidate track overlays",
            )
            assert requested_layers == ["residual", "average", "high-percentile"]

            page.locator("#doppler-overlays").uncheck()
            expect(page.locator("#doppler-canvas")).to_have_attribute(
                "aria-label",
                "High percentile (P99.0) Doppler waterfall for seg_doppler_b, receiver "
                "rx_doppler_b; 2 time bins by 3 frequency bins; "
                "0 candidate track overlays",
            )
        finally:
            browser.close()


@pytest.mark.parametrize(
    ("response_kind", "expected_state", "expected_badge"),
    [("pending", "pending", "Pending"), ("missing", "missing", "Unavailable")],
)
def test_browser_clears_stale_doppler_details_for_non_complete_states(
    response_kind: str,
    expected_state: str,
    expected_badge: str,
) -> None:
    with running_dashboard() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1200, "height": 900})

            def fulfill_doppler(route: Route) -> None:
                if response_kind == "missing":
                    route.fulfill(
                        status=404,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "error": {
                                    "code": "not_found",
                                    "message": "projection unavailable",
                                }
                            }
                        ),
                    )
                    return
                payload = visualization()
                payload = replace(
                    payload,
                    state=DopplerVisualizationState.PENDING,
                    waterfall_provenance=None,
                    doppler_provenance=(),
                    tiles=(),
                    candidates=(),
                    advanced_evidence=(),
                    reason_codes=("analysis-running",),
                )
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(json.loads(canonical_json_bytes(payload))),
                )

            page.route(
                f"**/api/v9/recordings/{RECORDING_ID}/doppler-visualization?*",
                fulfill_doppler,
            )
            response = page.goto(f"{base_url}/recordings/{RECORDING_ID}")
            assert response is not None and response.ok

            expect(page.locator("#doppler-state")).to_have_attribute(
                "data-state", expected_state
            )
            expect(page.locator("#doppler-candidate-count")).to_have_text(
                expected_badge
            )
            expect(page.locator("#doppler-figure")).to_be_hidden()
            expect(page.locator("#doppler-advanced")).to_be_hidden()
            expect(page.locator("#doppler-candidate-list")).to_be_empty()
            expect(page.locator("#doppler-coverage-facts")).to_be_empty()
        finally:
            browser.close()
