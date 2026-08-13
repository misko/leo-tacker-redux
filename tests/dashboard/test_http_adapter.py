from __future__ import annotations

import http.client
import json
import threading

import pytest

from leo_flow.adapters.dashboard_http import DashboardHttpError, StdlibDashboardServer
from leo_flow.dashboard.api import DashboardJsonApplication

from ._fixtures import repository


def _exchange(
    server: StdlibDashboardServer, target: str, *, method: str = "GET"
) -> tuple[int, dict[str, str], object]:
    application = DashboardJsonApplication(repository())
    worker = threading.Thread(target=server.serve_once, args=(application,))
    worker.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.bound_port, timeout=1)
    connection.request(method, target)
    response = connection.getresponse()
    payload = json.loads(response.read())
    headers = {name.casefold(): value for name, value in response.getheaders()}
    connection.close()
    worker.join(timeout=1)
    assert not worker.is_alive()
    return response.status, headers, payload


def test_loopback_ephemeral_listener_serves_dashboard_json_and_closes() -> None:
    server = StdlibDashboardServer(request_timeout_s=0.05)
    server.preflight("127.0.0.1", 0)
    status, headers, payload = _exchange(
        server,
        "/api/activity?start_utc_ns=100&stop_utc_ns=140&radio_ids=radio_a",
    )
    assert status == 200
    assert sum(item["count"] for item in payload["counts"]) == 4
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    server.close(0.1)
    with pytest.raises(DashboardHttpError, match="not bound"):
        _ = server.bound_port


def test_listener_rejects_remote_or_wildcard_binding_by_default() -> None:
    for host in ("0.0.0.0", "::", "dashboard.internal"):
        server = StdlibDashboardServer()
        with pytest.raises(DashboardHttpError, match="loopback"):
            server.preflight(host, 8080)


def test_serve_once_has_a_finite_idle_wait() -> None:
    server = StdlibDashboardServer(request_timeout_s=0.01)
    server.preflight("127.0.0.1", 0)
    assert not server.serve_once(DashboardJsonApplication(repository()))
    server.close(0.1)


def test_duplicate_query_and_mutating_method_are_deterministic_errors() -> None:
    duplicate = StdlibDashboardServer(request_timeout_s=0.05)
    duplicate.preflight("127.0.0.1", 0)
    status, _, payload = _exchange(
        duplicate,
        "/api/activity?start_utc_ns=100&start_utc_ns=101&stop_utc_ns=140",
    )
    assert status == 400
    assert payload["error"]["code"] == "invalid_http_request"
    duplicate.close(0.1)

    mutation = StdlibDashboardServer(request_timeout_s=0.05)
    mutation.preflight("127.0.0.1", 0)
    status, _, payload = _exchange(mutation, "/api/storage-health", method="POST")
    assert status == 405
    assert payload["error"]["code"] == "method_not_allowed"
    mutation.close(0.1)
