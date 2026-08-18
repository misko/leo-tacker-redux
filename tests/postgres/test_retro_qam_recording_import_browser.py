from __future__ import annotations

import io
import json
import re
import socket
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.contracts.core import canonical_json_bytes
from leo_flow.deployments import dashboard_v1, retro_qam_recording_import
from leo_flow.services.bootstrap import AdapterBuildContext, Capability, Process
from leo_flow.services.config import DashboardServiceConfig, RuntimeConfig
from leo_flow.services.lifecycle import NullDiagnosticSink
from tests.e2e.test_dashboard_browser import _browser_environment
from tests.e2e.test_retro_qam_canary_browser import _receipt
from tests.recording_import.test_retro_qam_import import _archive

_RECORDING_ID = "rec_retro_qam_20260813_clip002"
_TERMINAL_PANEL_STATE = re.compile(r"^(ready|pending|missing|empty|unavailable)$")


def _unused_port() -> int:
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return int(stream.getsockname()[1])


def _credential_directory(root: Path, dsn: str) -> Path:
    root.mkdir()
    (root / "catalog-dsn").write_text(dsn, encoding="utf-8")
    return root


def _run_import(postgres_dsn: str, tmp_path: Path) -> dict[str, object]:
    corpus_manifest, archive_root, manifest_digest = _archive(tmp_path)
    capture_credentials = _credential_directory(
        tmp_path / "capture-credentials", postgres_dsn
    )
    analysis_credentials = _credential_directory(
        tmp_path / "analysis-credentials", postgres_dsn
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    result = retro_qam_recording_import.main(
        [
            "--corpus-manifest",
            str(corpus_manifest),
            "--archive-root",
            str(archive_root),
            "--expected-manifest-sha256",
            manifest_digest.value,
            "--staging-root",
            str(tmp_path / "staging"),
            "--cas-root",
            str(tmp_path / "cas"),
            "--capture-credential-directory",
            str(capture_credentials),
            "--analysis-credential-directory",
            str(analysis_credentials),
            "--dashboard-base-url",
            "http://dashboard.invalid",
        ],
        stdout=stdout,
        stderr=stderr,
    )
    assert result == 0, stderr.getvalue()
    assert stderr.getvalue() == ""
    return json.loads(stdout.getvalue())


@contextmanager
def _running_dashboard(
    postgres_dsn: str,
    cas_root: Path,
    canary_receipt: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("LEO_RETRO_QAM_CANARY_RECEIPT", str(canary_receipt))
    context = AdapterBuildContext(
        Process.DASHBOARD,
        Capability.QUERY_PROJECTION,
        dashboard_v1.QUERY_PROJECTION_REF,
        {
            dashboard_v1.DATABASE_SECRET: postgres_dsn,
            dashboard_v1.CAS_ROOT_SECRET: str(cas_root),
        },
    )
    queries = dashboard_v1._postgres_query_projection(context)
    server = StdlibDashboardServer(
        request_timeout_s=0.01, maximum_concurrent_requests=8
    )
    port = _unused_port()
    config = DashboardServiceConfig(
        1,
        "dashboard",
        RuntimeConfig("retro-import-browser", 0.01, 1.0, ()),
        dashboard_v1.QUERY_PROJECTION_REF,
        dashboard_v1.SERVER_REF,
        "127.0.0.1",
        port,
    )
    service = dashboard_v1._build_dashboard(
        config,
        {
            Capability.QUERY_PROJECTION: queries,
            Capability.DASHBOARD_SERVER: server,
        },
        NullDiagnosticSink(),
    )
    # First bounded cycle performs the real read-only PostgreSQL preflight and
    # installs the fully composed V30 application before the browser starts.
    assert not service.run_once()
    stopped = threading.Event()

    def serve() -> None:
        while not stopped.is_set():
            service.run_once()

    worker = threading.Thread(target=serve, name="retro-import-real-dashboard")
    worker.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        stopped.set()
        worker.join(timeout=1)
        service.shutdown()
        assert not worker.is_alive()


def _assert_terminal(page, selector: str) -> None:  # type: ignore[no-untyped-def]
    state = page.locator(selector)
    expect(state).to_have_attribute("data-state", _TERMINAL_PANEL_STATE)
    expect(state).not_to_have_attribute("data-state", "loading")


@pytest.mark.integration
def test_imported_retro_recording_is_complete_in_real_dashboard_browser(
    postgres_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _run_import(postgres_dsn, tmp_path)
    assert receipt["recording_id"] == _RECORDING_ID
    assert receipt["historical_capture"] is True
    assert receipt["conditioned_canary"] is True
    assert receipt["calibrated_detection"] is False
    assert receipt["calibration_eligible"] is False

    canary_receipt = tmp_path / "latest.receipt.json"
    canary_receipt.write_bytes(canonical_json_bytes(_receipt()) + b"\n")
    with (
        _running_dashboard(
            postgres_dsn, tmp_path / "cas", canary_receipt, monkeypatch
        ) as base_url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch(
            headless=True, env=_browser_environment()
        )
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1400})
            # Keep the default two-hour catalog query centered on the immutable
            # historical timestamp rather than the wall clock of the test run.
            page.add_init_script(
                """
                (() => {
                  const NativeDate = Date;
                  const now = 1786659118595;
                  class FixedDate extends NativeDate {
                    constructor(...args) { super(...(args.length ? args : [now])); }
                    static now() { return now; }
                  }
                  FixedDate.parse = NativeDate.parse;
                  FixedDate.UTC = NativeDate.UTC;
                  globalThis.Date = FixedDate;
                })();
                """
            )
            response = page.goto(base_url)
            assert response is not None and response.ok

            expect(page.locator("#recordings-state")).to_have_attribute(
                "data-state", "ready"
            )
            row = page.locator("#recordings-body tr").filter(has_text=_RECORDING_ID)
            expect(row).to_have_count(1)
            cells = row.locator("th, td")
            expect(cells).to_have_count(5)
            expect(cells.nth(0)).to_contain_text(_RECORDING_ID)
            expect(cells.nth(0).locator(".full-capture-link")).to_have_attribute(
                "href", f"/recordings/{_RECORDING_ID}"
            )
            expect(cells.nth(1)).to_have_text("radio_historical_pluto_5d4d")
            expect(cells.nth(2)).to_contain_text("2026-08-13")
            expect(cells.nth(3)).to_have_text("test")
            expect(cells.nth(4)).to_have_text("pending")
            expect(row).not_to_contain_text("Loading")

            expect(page.locator("#retro-qam-canary-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#retro-qam-canary-recording-link")).to_have_attribute(
                "href", "/canaries/retro-qam/source-recording"
            )
            redirect = page.request.get(
                f"{base_url}/canaries/retro-qam/source-recording",
                max_redirects=0,
            )
            assert redirect.status == 302
            assert redirect.headers["location"] == f"/recordings/{_RECORDING_ID}"

            detail_response = page.goto(
                f"{base_url}/canaries/retro-qam/source-recording"
            )
            assert detail_response is not None and detail_response.ok
            expect(page).to_have_url(f"{base_url}/recordings/{_RECORDING_ID}")
            expect(page.locator("#capture-page-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#capture-detail-facts")).to_contain_text(
                "radio_historical_pluto_5d4d"
            )
            expect(page.locator("#segments-body")).to_contain_text(
                "seg_retro_qam_clip002"
            )
            expect(page.locator("#segments-body")).to_contain_text("test")
            expect(page.locator("#evidence-context-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#evidence-receivers")).to_contain_text(
                "rx_retro_qam_0"
            )
            expect(page.locator("#evidence-receivers")).to_contain_text(
                "rx_retro_qam_1"
            )

            page.locator("#evidence-load-extended").click()
            for selector in (
                "#evidence-receiver-agnostic-cfo-state",
                "#evidence-symbolwise-state",
                "#evidence-approaches-state",
                "#evidence-timeline-state",
                "#evidence-prescreen-state",
                "#evidence-qam-state",
                "#evidence-qam-combined-state",
                "#evidence-detector-state",
                "#evidence-doppler-state",
                "#evidence-pilot-doppler-state",
                "#waterfall-state",
                "#doppler-state",
                "#surrogate-state",
                "#constellation-state",
                "#temporal-state",
                "#analysis-state",
            ):
                _assert_terminal(page, selector)
            expect(
                page.locator('#evidence-workspace [data-state="loading"]')
            ).to_have_count(0)
        finally:
            browser.close()
