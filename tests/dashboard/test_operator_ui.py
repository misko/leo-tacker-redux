from __future__ import annotations

import http.client
import threading

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV2,
    JsonRequest,
)
from leo_flow.dashboard.ui import DashboardUiApplication
from leo_flow.deployments import dashboard_v1
from leo_flow.services.bootstrap import Capability
from leo_flow.services.config import (
    DashboardServiceConfig,
    RuntimeConfig,
    SecretRef,
)

from ._fixtures import repository


def application() -> DashboardUiApplication:
    queries = repository()
    return DashboardUiApplication(DashboardJsonApplicationV2(queries, queries))


def test_static_routes_are_exact_allow_list_with_safe_content_and_cache_policy() -> (
    None
):
    app = application()
    expected = {
        "/": ("text/html; charset=utf-8", "no-store"),
        "/assets/dashboard.css": ("text/css; charset=utf-8", "public, max-age=300"),
        "/assets/dashboard.js": (
            "text/javascript; charset=utf-8",
            "public, max-age=300",
        ),
        "/aggregate-stats": ("text/html; charset=utf-8", "no-store"),
        "/assets/aggregate-stats.js": (
            "text/javascript; charset=utf-8",
            "public, max-age=300",
        ),
        "/aggregate-doppler": ("text/html; charset=utf-8", "no-store"),
        "/assets/aggregate-doppler.js": (
            "text/javascript; charset=utf-8",
            "public, max-age=300",
        ),
        "/full-dwell": ("text/html; charset=utf-8", "no-store"),
        "/assets/full-dwell.js": (
            "text/javascript; charset=utf-8",
            "public, max-age=300",
        ),
    }
    for path, (content_type, cache_control) in expected.items():
        response = app.handle(JsonRequest("GET", path, {}))
        headers = dict(response.headers)
        assert response.status == 200
        assert response.body
        assert headers["content-type"] == content_type
        assert headers["cache-control"] == cache_control
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["x-frame-options"] == "DENY"
        assert headers["cross-origin-resource-policy"] == "same-origin"
        assert headers["permissions-policy"] == (
            "camera=(), microphone=(), geolocation=()"
        )
        assert "default-src 'self'" in headers["content-security-policy"]
        assert "object-src 'none'" in headers["content-security-policy"]
        assert "frame-ancestors 'none'" in headers["content-security-policy"]

    traversal = app.handle(JsonRequest("GET", "/assets/../dashboard/api.py", {}))
    unknown = app.handle(JsonRequest("GET", "/index.html", {}))
    assert traversal.status == unknown.status == 404
    assert b"api.py" not in traversal.body
    assert traversal.body == b"Not found\n"


def test_overview_discloses_duty_candidate_and_calibration_semantics() -> None:
    app = application()
    html = app.handle(JsonRequest("GET", "/", {})).body.decode()
    javascript = app.handle(
        JsonRequest("GET", "/assets/dashboard.js", {})
    ).body.decode()
    for required in (
        'id="duty-cycle-table"',
        'id="starlink-rate-table"',
        'id="capture-starlink-filter"',
        "Calibrated beacon detections unavailable",
    ):
        assert required in html
    assert "/api/v6/observation-aggregate" in javascript
    assert "Candidate-positive means score > conditioned control" in javascript
    assert 'starlinkFilter === "detected"' in javascript


def test_every_user_facing_page_links_the_top_level_analysis_views() -> None:
    app = application()
    for path in (
        "/",
        "/aggregate-stats",
        "/aggregate-doppler",
        "/full-dwell",
        "/recordings/rec_1",
    ):
        html = app.handle(JsonRequest("GET", path, {})).body.decode()
        assert 'href="/aggregate-stats"' in html
        assert 'href="/aggregate-doppler"' in html
        assert 'href="/full-dwell"' in html


def test_aggregate_stats_page_has_bounded_density_controls_and_safe_rendering() -> None:
    app = application()
    html = app.handle(JsonRequest("GET", "/aggregate-stats", {})).body.decode()
    javascript = app.handle(
        JsonRequest("GET", "/assets/aggregate-stats.js", {})
    ).body.decode()
    for required in (
        'id="density-window-hours"',
        'id="method-selector"',
        'id="density-radio"',
        'id="density-receiver"',
        'id="density-edge"',
        'id="show-surrogate"',
        'id="density-canvas"',
        'id="score-summary-table"',
        'id="temporal-summary-table"',
        'id="temporal-aggregate-state"',
        "first successful dwell from the continuous service",
        "identical bounded search",
        "not yet a calibrated population null",
        "Precommitted surrogate scores",
    ):
        assert required in html
    assert "/api/v12/surrogate-score-distributions" in javascript
    assert "/api/v13/temporal-pilot-aggregate" in javascript
    assert "Mean probe maximum" in html
    assert "CONTINUOUS_SAMPLE_START_UTC_NS" in javascript
    assert "visibleMethods" in javascript
    assert "unit histogram area" in html
    assert "recording + segment + radio + RX chain" in html
    assert "innerHTML" not in javascript
    assert "eval(" not in javascript
    assert "http://" not in html and "https://" not in html


def test_aggregate_doppler_page_separates_candidate_sources_and_controls() -> None:
    app = application()
    html = app.handle(JsonRequest("GET", "/aggregate-doppler", {})).body.decode()
    javascript = app.handle(
        JsonRequest("GET", "/assets/aggregate-doppler.js", {})
    ).body.decode()
    for required in (
        'id="drift-canvas"',
        'class="detail-card doppler-density-wide"',
        'id="control-canvas"',
        'id="source-filters"',
        "Candidate-only motion evidence",
        "Radios and receiver ports are never pooled",
    ):
        assert required in html
    for required in (
        "/api/v14/doppler-aggregate",
        'category === "source"',
        'category === "radio"',
        'category === "receiver"',
        'category === "method"',
        'category === "model"',
        'category === "control"',
        "radio-and-receiver-series-are-never-pooled",
    ):
        assert required in javascript
    assert "innerHTML" not in javascript
    assert "eval(" not in javascript
    assert "http://" not in html and "https://" not in html


def test_dedicated_recording_page_is_same_origin_bounded_and_path_validated() -> None:
    app = application()
    page = app.handle(JsonRequest("GET", "/recordings/rec_1", {}))
    html = page.body.decode()
    assert page.status == 200
    assert dict(page.headers)["cache-control"] == "no-store"
    for required in (
        'id="capture-detail-main"',
        'aria-labelledby="capture-facts-heading"',
        'aria-labelledby="segments-heading"',
        'aria-labelledby="waterfall-heading"',
        'aria-labelledby="analysis-heading"',
        'id="starlink-decision"',
        'id="diagnostic-features" class="diagnostic-features"',
        'aria-controls="diagnostic-features-panel" aria-expanded="false"',
        'id="waterfall-canvas"',
        'id="waterfall-tile"',
        'id="temporal-chart"',
        'id="temporal-radio"',
        'id="temporal-receiver"',
        "not continuous coverage",
        '<script src="/assets/recording-detail.js" defer></script>',
    ):
        assert required in html
    assert "http://" not in html and "https://" not in html
    assert (
        app.handle(JsonRequest("GET", "/recordings/not-a-recording", {})).status == 404
    )
    assert app.handle(JsonRequest("GET", "/recordings/rec_1/extra", {})).status == 404
    assert app.handle(JsonRequest("GET", "/recordings/rec_1%2Fextra", {})).status == 404


def test_recording_page_script_uses_only_projected_json_and_safe_dom_apis() -> None:
    javascript = (
        application()
        .handle(JsonRequest("GET", "/assets/recording-detail.js", {}))
        .body.decode()
    )
    for route in (
        "/api/v3/recordings/",
        "/api/v4/recordings/",
        "/api/v13/recordings/",
        "/waterfall",
        "/api/recordings/",
        "/features?selector=*",
    ):
        assert route in javascript
    assert "/api/v3/recordings/${encodeURIComponent(recordingId)}/starlink" not in (
        javascript
    )
    assert "getContext" in javascript and "fillRect" in javascript
    assert "innerHTML" not in javascript
    assert "/home/" not in javascript
    assert "cas:" not in javascript.casefold()
    assert "fetch(" in javascript


def test_head_and_json_delegation_preserve_existing_api_schema() -> None:
    app = application()
    head = app.handle(JsonRequest("HEAD", "/", {}))
    assert head.status == 200
    assert head.body.startswith(b"<!doctype html>")

    api = app.handle(JsonRequest("GET", "/api/storage-health", {}))
    assert api.status == 200
    assert dict(api.headers) == {"content-type": "application/json; charset=utf-8"}
    assert api.body == b'{"available":true,"free_bytes":250,"total_bytes":1000}'

    mutation = app.handle(JsonRequest("POST", "/", {}))
    assert mutation.status == 405
    assert dict(mutation.headers)["content-type"] == "application/json; charset=utf-8"


def test_html_has_keyboard_landmarks_labels_and_explicit_state_hooks() -> None:
    html = application().handle(JsonRequest("GET", "/", {})).body.decode()
    for required in (
        'class="skip-link" href="#main-content"',
        "<header",
        '<nav class="section-nav" aria-label="Dashboard sections"',
        '<main id="main-content" tabindex="-1"',
        'aria-labelledby="overview-heading"',
        'aria-labelledby="capture-batches-heading"',
        'aria-label="Capture table filters"',
        '<label for="capture-window-hours"',
        '<label for="capture-radio-filter"',
        'id="capture-batches-table"',
        '<th scope="col">UTC</th>',
        '<th scope="col">Pilot beacons<br>',
        '<th scope="col">Capture time</th>',
        '<th scope="col">Satellites tracked</th>',
        'id="capture-attempts-body"',
        'id="capture-batches-more"',
        'aria-labelledby="recordings-heading"',
        'aria-labelledby="evaluation-heading"',
        'aria-labelledby="models-tracks-heading"',
        'aria-labelledby="storage-heading"',
        '<label for="window-hours"',
        '<label for="evaluation-id"',
        '<label for="model-id"',
        '<option value="8">8 hours</option>',
        '<option value="168">7 days</option>',
        '<option value="720">30 days</option>',
        'aria-live="polite"',
        "<caption>",
        'data-state="loading"',
        'data-state="empty"',
    ):
        assert required in html
    assert "<script src=" in html
    assert "<script>" not in html
    assert "<style" not in html
    assert "http://" not in html and "https://" not in html


def test_retro_qam_source_recording_link_redirects_to_historical_detail() -> None:
    response = application().handle(
        JsonRequest("GET", "/canaries/retro-qam/source-recording", {})
    )
    assert response.status == 302
    assert dict(response.headers)["location"] == (
        "http://satpi01:8765/recordings/beacon/"
        "ch4-lower-edge-narrow-pluto-5d4d-20260813T211014Z"
    )


def test_retro_qam_has_native_detail_page_and_report_link() -> None:
    app = application()
    page = app.handle(JsonRequest("GET", "/canaries/retro-qam", {}))
    assert page.status == 200
    html = page.body.decode()
    assert "RETRO QAM canary" in html
    assert "/canaries/retro-qam/source-recording" in html
    assert "/canaries/retro-qam/report" in html
    assert (
        app.handle(JsonRequest("GET", "/assets/retro-qam-canary.js", {})).status == 200
    )
    report = app.handle(JsonRequest("GET", "/canaries/retro-qam/report", {}))
    assert report.status == 302
    assert dict(report.headers)["location"].endswith(
        "reports/qam_retro_investigation.md"
    )


def test_ui_assets_expose_empty_loading_error_ready_stale_and_missing_states() -> None:
    app = application()
    javascript = app.handle(
        JsonRequest("GET", "/assets/dashboard.js", {})
    ).body.decode()
    css = app.handle(JsonRequest("GET", "/assets/dashboard.css", {})).body.decode()

    for state in ("empty", "loading", "error", "ready", "stale", "missing"):
        assert f'"{state}"' in javascript or f'[data-state="{state}"]' in css
    for route in (
        "/api/activity",
        "/api/v2/capture-batches",
        "/api/v3/recordings/",
        "/api/recordings",
        "/api/evaluations/",
        "/api/models/",
        "/api/tracks",
        "/api/storage-health",
    ):
        assert route in javascript
    assert "Promise.allSettled" in javascript
    assert "captureBatchCursor" in javascript
    assert "encodeURIComponent(nextCursor)" in javascript
    assert "captureRows" in javascript
    assert "Searching all stable pages for this radio" in javascript
    assert "View capture details, waterfall, and analysis" in javascript
    assert "makeCaptureRowNavigable" in javascript
    assert "formatCompactUtcNs" in javascript
    assert "MAX_CAPTURE_DURATION_LOADS = 4" in javascript
    assert (
        "Calibrated Anchor-8 and GLRT beacon detections are unavailable" in javascript
    )
    assert (
        'event.target.closest("a, button, input, select, textarea, summary")'
        in javascript
    )
    assert '["Enter", " "]' in javascript
    assert "RADIO_DISPLAY_ALIASES_V1" in javascript
    assert (
        'radio_pluto_5d4d: Object.freeze({ short: ".20", '
        'address: "192.168.1.20" })' in javascript
    )
    assert (
        'radio_pluto_19f2: Object.freeze({ short: ".21", '
        'address: "192.168.1.21" })' in javascript
    )
    assert "radioMatchesFilter" in javascript and "radioDisplayName" in javascript
    assert "start_utc_ns=" in javascript and "stop_utc_ns=" in javascript
    assert "stop is exclusive" in javascript
    assert "innerHTML" not in javascript
    assert "/home/" not in javascript and ".png" not in javascript.lower()
    assert "@media (max-width: 58rem)" in css
    assert "@media (max-width: 42rem)" in css
    assert "prefers-reduced-motion" in css


def test_same_listener_serves_ui_with_security_headers_and_json_no_store() -> None:
    server = StdlibDashboardServer(request_timeout_s=0.05)
    server.preflight("127.0.0.1", 0)
    app = application()

    def exchange(path: str) -> tuple[int, dict[str, str], bytes]:
        worker = threading.Thread(target=server.serve_once, args=(app,))
        worker.start()
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.bound_port, timeout=1
        )
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        headers = {name.casefold(): value for name, value in response.getheaders()}
        connection.close()
        worker.join(timeout=1)
        assert not worker.is_alive()
        return response.status, headers, body

    ui_status, ui_headers, ui_body = exchange("/assets/dashboard.css")
    assert ui_status == 200 and ui_body
    assert ui_headers["content-type"] == "text/css; charset=utf-8"
    assert ui_headers["cache-control"] == "public, max-age=300"
    assert ui_headers["x-content-type-options"] == "nosniff"
    assert ui_headers["content-security-policy"].startswith("default-src 'self'")

    api_status, api_headers, api_body = exchange("/api/storage-health")
    assert api_status == 200 and api_body
    assert api_headers["content-type"] == "application/json; charset=utf-8"
    assert api_headers["cache-control"] == "no-store"
    server.close(0.1)


def test_dashboard_v1_composition_serves_ui_without_adding_a_service() -> None:
    class CapturingServer:
        def __init__(self) -> None:
            self.handler = None

        def preflight(self, bind_host: str, bind_port: int) -> None:
            assert (bind_host, bind_port) == ("127.0.0.1", 8090)

        def serve_once(self, handler) -> bool:
            self.handler = handler
            return True

        def close(self, timeout_s: float) -> None:
            assert timeout_s == 1.0

    class Queries:
        def storage_health(self):
            return repository().storage_health()

    class Diagnostics:
        def emit(self, event) -> None:
            del event

    config = DashboardServiceConfig(
        1,
        "dashboard",
        RuntimeConfig(
            "dashboard-ui-test",
            0.01,
            1.0,
            (SecretRef(dashboard_v1.SECRET_PROVIDER, "catalog-dsn"),),
        ),
        dashboard_v1.QUERY_PROJECTION_REF,
        dashboard_v1.SERVER_REF,
        "127.0.0.1",
        8090,
    )
    server = CapturingServer()
    service = dashboard_v1._build_dashboard(
        config,
        {
            Capability.QUERY_PROJECTION: Queries(),
            Capability.DASHBOARD_SERVER: server,
        },
        Diagnostics(),
    )

    assert service.run_once()
    assert isinstance(server.handler, DashboardUiApplication)
    assert server.handler.handle(JsonRequest("GET", "/", {})).status == 200
    assert (
        server.handler.handle(JsonRequest("GET", "/api/storage-health", {})).status
        == 200
    )
    service.shutdown()
