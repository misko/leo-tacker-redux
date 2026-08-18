from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from playwright.sync_api import expect, sync_playwright

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.contracts.core import RadioId, RecordingId
from leo_flow.dashboard import DashboardNotFound
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV14,
    DashboardJsonApplicationV15,
    DashboardJsonApplicationV16,
    DashboardJsonApplicationV17,
    DashboardJsonApplicationV19,
    JsonRequest,
    JsonResponse,
)
from leo_flow.dashboard.ui import DashboardUiApplication

REQUESTED = "rec_e2e_radio_a"
COMPANION = "rec_e2e_radio_b"


class _NotFoundApplication:
    """Only the evidence APIs are relevant to this focused browser composition."""

    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(
            404,
            (("content-type", "application/json; charset=utf-8"),),
            b'{"error":{"code":"not_found","message":"fixture route absent"}}',
        )


class EvidencePorts:
    def __init__(
        self,
        *,
        qam_state: str = "complete",
        detector_state: str = "complete",
        basic_doppler_state: str = "complete",
        advanced_doppler_state: str = "complete",
        missing_qam_recordings: frozenset[str] = frozenset(),
        missing_detector_recordings: frozenset[str] = frozenset(),
    ) -> None:
        self.qam_state = qam_state
        self.detector_state = detector_state
        self.basic_doppler_state = basic_doppler_state
        self.advanced_doppler_state = advanced_doppler_state
        self.missing_qam_recordings = missing_qam_recordings
        self.missing_detector_recordings = missing_detector_recordings
        self.qam_queries: list[Any] = []
        self.detector_queries: list[Any] = []
        self.basic_doppler_queries: list[Any] = []
        self.advanced_doppler_queries: list[Any] = []

    def recording_evidence_context(self, recording_id: RecordingId) -> dict[str, Any]:
        assert str(recording_id) == REQUESTED
        return {
            "requested_recording_id": REQUESTED,
            "capture_batch_id": "cbatch_evidence_e2e",
            "recordings": [
                {
                    "recording_id": REQUESTED,
                    "radio_id": "radio_a",
                    "radio_serial": "serial-a",
                    "analysis_state": "complete",
                    "requested": True,
                },
                {
                    "recording_id": COMPANION,
                    "radio_id": "radio_b",
                    "radio_serial": "serial-b",
                    "analysis_state": "complete",
                    "requested": False,
                },
            ],
            "receivers": [
                {
                    "recording_id": REQUESTED,
                    "radio_id": "radio_a",
                    "receiver_chain_id": "rx_a",
                    "lnb_id": "lnb_authoritative_a",
                    "radio_channel": 0,
                },
                {
                    "recording_id": COMPANION,
                    "radio_id": "radio_b",
                    "receiver_chain_id": "rx_b",
                    "lnb_id": "lnb_authoritative_b",
                    "radio_channel": 0,
                },
            ],
            "segments": [
                {
                    "recording_id": REQUESTED,
                    "segment_id": "seg_a",
                    "receiver_chain_ids": ["rx_a"],
                },
                {
                    "recording_id": COMPANION,
                    "segment_id": "seg_b",
                    "receiver_chain_ids": ["rx_b"],
                },
            ],
            "candidate_only": True,
            "calibrated_detection_count": None,
            "warnings": ["candidate-only-evidence-not-calibrated-detection"],
            "limitations": [],
        }

    def recording_starlink_acquired_constellation(self, query: Any) -> dict[str, Any]:
        self.qam_queries.append(query)
        if str(query.recording_id) in self.missing_qam_recordings:
            raise DashboardNotFound("acquired QAM recording was not found")
        self._raise_for_state(self.qam_state, "acquired QAM")
        identity = _identity(str(query.recording_id))
        if identity is None:
            raise DashboardNotFound("acquired QAM recording was not found")
        radio, receiver, lnb, segment = identity
        if not _matches(query.radio_ids, radio) or not _matches(
            query.receiver_chain_ids, receiver
        ):
            streams: list[dict[str, Any]] = []
        elif (query.lnb_ids and lnb not in query.lnb_ids) or (
            query.edges and "lower" not in {str(value.value) for value in query.edges}
        ):
            streams = []
        else:
            window_count = 1 if query.mode.value == "overall" else 2
            streams = [
                {
                    "recording_id": str(query.recording_id),
                    "radio_id": radio,
                    "lnb_id": lnb,
                    "segment_id": segment,
                    "receiver_chain_id": receiver,
                    "edge": "lower",
                    "overall": {
                        "window_count": 2,
                        "selected_display_window_index": 0,
                        "support_weighted_hard_symbol_accuracy": 0.84,
                    },
                    "windows": [
                        {
                            "window_index": index,
                            "start_sample": index * 25_000,
                            "stop_sample": (index + 1) * 25_000,
                            "interval_start_utc_ns": 1_000_000_000 + index * 10_000_000,
                            "interval_stop_utc_ns": 1_010_000_000 + index * 10_000_000,
                            "display_points": [
                                {"i": 0.70, "q": 0.72, "expected_state": 0},
                                {"i": -0.71, "q": 0.69, "expected_state": 1},
                            ],
                        }
                        for index in range(window_count)
                    ],
                }
            ]
        return {
            "recording_id": str(query.recording_id),
            "mode": query.mode.value,
            "candidate_only": True,
            "calibration_required": True,
            "streams": streams,
        }

    def recording_starlink_full_dwell(self, query: Any) -> dict[str, Any]:
        self.detector_queries.append(query)
        if str(query.recording_id) in self.missing_detector_recordings:
            raise DashboardNotFound("full-dwell recording was not found")
        self._raise_for_state(self.detector_state, "full-dwell detector")
        identity = _identity(str(query.recording_id))
        if identity is None:
            raise DashboardNotFound("full-dwell recording was not found")
        radio, receiver, _lnb, segment = identity
        if not _matches(query.radio_ids, radio) or not _matches(
            query.receiver_chain_ids, receiver
        ):
            streams: list[dict[str, Any]] = []
        elif query.edges and "lower" not in {str(value.value) for value in query.edges}:
            streams = []
        else:
            points = []
            for method_offset, method in enumerate(query.methods):
                for window_index in range(2):
                    qin = 0.84 - method_offset * 0.08 - window_index * 0.02
                    points.append(
                        {
                            "method": method.value,
                            "interval_start_utc_ns": 1_000_000_000
                            + window_index * 10_000_000,
                            "interval_stop_utc_ns": 1_010_000_000
                            + window_index * 10_000_000,
                            "qin": {"score": qin},
                            "surrogates": [
                                {"winner": {"score": qin - 0.25}},
                                {"winner": {"score": qin - 0.30}},
                            ],
                        }
                    )
            streams = [
                {
                    "radio_id": radio,
                    "segment_id": segment,
                    "receiver_chain_id": receiver,
                    "channel_number": 4,
                    "edge": "lower",
                    "points": points,
                }
            ]
        return {
            "recording_id": str(query.recording_id),
            "queue_state": "complete",
            "candidate_only": True,
            "calibrated_detection_count": None,
            "streams": streams,
        }

    def recording_evidence_doppler(self, query: Any) -> dict[str, Any]:
        self.basic_doppler_queries.append(query)
        return self._doppler(query, self.basic_doppler_state, advanced=False)

    def recording_evidence_advanced_doppler(self, query: Any) -> dict[str, Any]:
        self.advanced_doppler_queries.append(query)
        return self._doppler(query, self.advanced_doppler_state, advanced=True)

    def _doppler(
        self, query: Any, product_state: str, *, advanced: bool
    ) -> dict[str, Any]:
        self._raise_for_state(product_state, "Doppler")
        if product_state == "missing":
            return _empty_doppler(product_state)
        series = []
        for recording in (REQUESTED, COMPANION):
            identity = _identity(recording)
            assert identity is not None
            radio, receiver, lnb, segment = identity
            if not _matches(query.radio_ids, radio):
                continue
            if not _matches(query.receiver_chain_ids, receiver):
                continue
            if query.lnb_ids and lnb not in query.lnb_ids:
                continue
            drift = (12_500.0 if radio == "radio_a" else -9_500.0) * (
                -1 if advanced else 1
            )
            windows = [
                {
                    "window_index": index,
                    "start_sample": index * 25_000,
                    "stop_sample": (index + 1) * 25_000,
                    "interval_start_utc_ns": 1_000_000_000 + index * 10_000_000,
                    "interval_stop_utc_ns": 1_010_000_000 + index * 10_000_000,
                    "point_start_utc_ns": 1_002_000_000 + index * 10_000_000,
                    "point_stop_utc_ns": 1_008_000_000 + index * 10_000_000,
                    "drift_rate_hz_s": drift + index * 200.0,
                    "midpoint_frequency_hz": 10_755_000_000.0,
                    "support_count": 2,
                }
                for index in range(2)
            ]
            item: dict[str, Any] = {
                "recording_id": recording,
                "radio_id": radio,
                "lnb_id": lnb,
                "receiver_chain_id": receiver,
                "segment_id": segment,
                "total": {"drift_rate_hz_s": drift},
                "windows": windows,
            }
            if advanced:
                item["association_state"] = "advanced-path-only"
                item["path_digest"] = "a" * 64
            else:
                item["candidate_rank"] = 1
            series.append(item)
        return {
            "requested_recording_id": REQUESTED,
            "state": "complete" if series else "missing",
            "candidate_only": True,
            "calibrated_detection_count": None,
            "series": series,
            "original_window_count": 2 * len(series),
            "truncated": False,
            "warnings": ["candidate-only-evidence-not-calibrated-detection"],
        }

    @staticmethod
    def _raise_for_state(product_state: str, label: str) -> None:
        if product_state == "pending":
            raise DashboardNotFound(f"{label} is pending")
        if product_state == "error":
            raise RuntimeError(f"{label} failed")


def _empty_doppler(state: str) -> dict[str, Any]:
    return {
        "requested_recording_id": REQUESTED,
        "state": state,
        "candidate_only": True,
        "calibrated_detection_count": None,
        "series": [],
        "original_window_count": 0,
        "truncated": False,
        "warnings": ["candidate-only-evidence-not-calibrated-detection"],
    }


def _identity(recording_id: str) -> tuple[str, str, str, str] | None:
    return {
        REQUESTED: ("radio_a", "rx_a", "lnb_authoritative_a", "seg_a"),
        COMPANION: ("radio_b", "rx_b", "lnb_authoritative_b", "seg_b"),
    }.get(recording_id)


def _matches(values: tuple[Any, ...], expected: str) -> bool:
    return not values or expected in {str(value) for value in values}


def _application(ports: EvidencePorts) -> DashboardUiApplication:
    base = cast(DashboardJsonApplicationV14, _NotFoundApplication())
    fixture_port = cast(Any, ports)
    v15 = DashboardJsonApplicationV15(base, fixture_port)
    v16 = DashboardJsonApplicationV16(v15, fixture_port, fixture_port)
    v17 = DashboardJsonApplicationV17(v16, fixture_port)
    return DashboardUiApplication(DashboardJsonApplicationV19(v17, fixture_port))


@contextmanager
def running_dashboard(ports: EvidencePorts) -> Iterator[str]:
    server = StdlibDashboardServer(request_timeout_s=0.01)
    server.preflight("127.0.0.1", 0)
    application = _application(ports)
    stopped = threading.Event()

    def serve() -> None:
        while not stopped.is_set():
            server.serve_once(application)

    worker = threading.Thread(target=serve, name="recording-evidence-e2e-server")
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


def test_detail_page_renders_and_filters_all_populated_candidate_evidence() -> None:
    ports = EvidencePorts()
    with running_dashboard(ports) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1300})
            response = page.goto(f"{base_url}/recordings/{REQUESTED}")
            assert response is not None and response.ok

            expect(page.locator("#evidence-warning")).to_contain_text(
                "Candidate-only evidence"
            )
            expect(page.locator("#evidence-warning")).to_contain_text("never pooled")
            expect(page.locator("#evidence-context-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#evidence-context-state")).to_contain_text(
                "2 recording scope"
            )
            for product in ("qam", "detector", "doppler"):
                expect(page.locator(f"#evidence-{product}-state")).to_have_attribute(
                    "data-state", "ready"
                )
                expect(page.locator(f"#evidence-{product}-badge")).to_have_text(
                    "Candidate evidence"
                )
                expect(page.locator(f"#evidence-{product}-canvas")).to_be_visible()

            expect(page.locator("#evidence-qam-heading")).to_have_text("Pilot QAM")
            expect(page.locator("#evidence-detector-heading")).to_have_text(
                "Starlink algorithm scores"
            )
            expect(page.locator("#evidence-doppler-heading")).to_have_text(
                "Doppler estimates"
            )
            expect(page.locator("#evidence-qam-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            expect(page.locator("#evidence-detector-canvas")).to_have_attribute(
                "data-series-count", "12"
            )
            expect(page.locator("#evidence-doppler-canvas")).to_have_attribute(
                "data-series-count", "4"
            )
            expect(page.locator("#evidence-qam-legend")).to_contain_text(
                f"{REQUESTED} · radio_a · lnb_authoritative_a · rx_a · lower · overall"
            )
            expect(page.locator("#evidence-detector-legend")).to_contain_text(
                f"{COMPANION} · radio_b · lnb_authoritative_b · rx_b · CH4 · lower · glrt-32 · qin"
            )
            expect(page.locator("#evidence-doppler-legend")).to_contain_text(
                "basic candidate 1"
            )
            expect(page.locator("#evidence-doppler-legend")).to_contain_text(
                "advanced path only"
            )
            expect(page.locator("#evidence-doppler-state")).to_contain_text(
                "4 unpooled series (2 basic candidate, 2 advanced-path-only)"
            )

            page.locator("#evidence-mode").select_option("windows")
            expect(page.locator("#evidence-qam-canvas")).to_have_attribute(
                "data-series-count", "4"
            )
            expect(page.locator("#evidence-detector-canvas")).to_have_attribute(
                "data-point-count", "24"
            )
            expect(page.locator("#evidence-doppler-canvas")).to_have_attribute(
                "data-point-count", "8"
            )
            expect(page.locator("#evidence-detector-state")).to_contain_text(
                "each point is one exact analyzed window"
            )
            expect(page.locator("#evidence-doppler-state")).to_contain_text(
                "explicit UTC/sample scope"
            )
            assert {query.mode.value for query in ports.qam_queries} == {
                "overall",
                "windows",
            }
            assert all(query.maximum_streams == 4 for query in ports.qam_queries)
            assert all(
                query.maximum_windows_per_stream == 32 for query in ports.qam_queries
            )

            page.locator('#evidence-methods input[value="anchor-8"]').uncheck()
            expect(page.locator("#evidence-detector-canvas")).to_have_attribute(
                "data-series-count", "8"
            )
            assert all(
                method.value != "anchor-8"
                for method in ports.detector_queries[-1].methods
            )
            page.locator('#evidence-methods input[value="anchor-8"]').check()

            page.locator('#evidence-patterns input[value="surrogate-0"]').uncheck()
            expect(page.locator("#evidence-detector-canvas")).to_have_attribute(
                "data-series-count", "6"
            )
            expect(page.locator("#evidence-detector-legend")).not_to_contain_text(
                "surrogate-0"
            )
            page.locator('#evidence-patterns input[value="surrogate-0"]').check()

            page.locator(f'#evidence-radios input[value="{COMPANION}"]').uncheck()
            expect(page.locator("#evidence-qam-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            expect(page.locator("#evidence-detector-canvas")).to_have_attribute(
                "data-series-count", "6"
            )
            expect(page.locator("#evidence-doppler-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            expect(page.locator("#evidence-qam-legend")).not_to_contain_text(COMPANION)
            assert ports.basic_doppler_queries[-1].radio_ids == (RadioId("radio_a"),)
            page.locator(f'#evidence-radios input[value="{COMPANION}"]').check()

            page.locator('#evidence-lnbs input[value="lnb_authoritative_b"]').uncheck()
            expect(page.locator("#evidence-qam-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            expect(page.locator("#evidence-doppler-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            expect(page.locator("#evidence-qam-legend")).not_to_contain_text(
                "lnb_authoritative_b"
            )
            page.locator('#evidence-lnbs input[value="lnb_authoritative_b"]').check()

            page.locator('#evidence-receivers input[value="rx_b"]').uncheck()
            expect(page.locator("#evidence-qam-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            expect(page.locator("#evidence-doppler-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            page.locator('#evidence-receivers input[value="rx_b"]').check()

            page.locator('#evidence-channels input[value="4"]').uncheck()
            expect(page.locator("#evidence-detector-state")).to_have_attribute(
                "data-state", "missing"
            )
            expect(page.locator("#evidence-detector-canvas")).to_be_hidden()
            page.locator('#evidence-channels input[value="4"]').check()

            page.locator('#evidence-edges input[value="lower"]').uncheck()
            expect(page.locator("#evidence-qam-state")).to_have_attribute(
                "data-state", "missing"
            )
            expect(page.locator("#evidence-detector-state")).to_have_attribute(
                "data-state", "missing"
            )
            expect(page.locator("#evidence-doppler-state")).to_have_attribute(
                "data-state", "ready"
            )
            page.locator('#evidence-edges input[value="lower"]').check()
        finally:
            browser.close()


def test_detail_page_distinguishes_pending_error_and_missing_real_api_states() -> None:
    ports = EvidencePorts(
        qam_state="pending",
        detector_state="error",
        basic_doppler_state="missing",
        advanced_doppler_state="missing",
    )
    with running_dashboard(ports) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page()
            response = page.goto(f"{base_url}/recordings/{REQUESTED}")
            assert response is not None and response.ok
            expect(page.locator("#evidence-qam-state")).to_have_attribute(
                "data-state", "pending"
            )
            expect(page.locator("#evidence-qam-state")).to_contain_text(
                "pending for every selected recording"
            )
            expect(page.locator("#evidence-detector-state")).to_have_attribute(
                "data-state", "error"
            )
            expect(page.locator("#evidence-detector-state")).to_contain_text(
                "dashboard query failed"
            )
            expect(page.locator("#evidence-doppler-state")).to_have_attribute(
                "data-state", "missing"
            )
            expect(page.locator("#evidence-doppler-state")).to_contain_text(
                "Doppler evidence is missing"
            )
            for product in ("qam", "detector", "doppler"):
                expect(page.locator(f"#evidence-{product}-canvas")).to_be_hidden()
        finally:
            browser.close()


def test_detail_page_renders_available_recording_when_companion_is_pending() -> None:
    ports = EvidencePorts(
        missing_qam_recordings=frozenset({COMPANION}),
        missing_detector_recordings=frozenset({COMPANION}),
    )
    with running_dashboard(ports) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page()
            response = page.goto(f"{base_url}/recordings/{REQUESTED}")
            assert response is not None and response.ok
            for product in ("qam", "detector"):
                expect(page.locator(f"#evidence-{product}-state")).to_have_attribute(
                    "data-state", "ready"
                )
                expect(page.locator(f"#evidence-{product}-state")).to_contain_text(
                    "1 selected recording(s) remain pending"
                )
                expect(page.locator(f"#evidence-{product}-canvas")).to_be_visible()
            expect(page.locator("#evidence-qam-legend")).to_contain_text(REQUESTED)
            expect(page.locator("#evidence-qam-legend")).not_to_contain_text(
                COMPANION
            )
            expect(page.locator("#evidence-detector-legend")).to_contain_text(
                REQUESTED
            )
            expect(page.locator("#evidence-detector-legend")).not_to_contain_text(
                COMPANION
            )
        finally:
            browser.close()
