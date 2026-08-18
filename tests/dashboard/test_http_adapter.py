from __future__ import annotations

import http.client
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from leo_flow.adapters.dashboard_http import DashboardHttpError, StdlibDashboardServer
from leo_flow.contracts.core import canonical_json_bytes
from leo_flow.dashboard.api import (
    DashboardJsonApplication,
    JsonRequest,
    JsonResponse,
)

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


def test_explicit_remote_listener_permits_ipv4_wildcard() -> None:
    server = StdlibDashboardServer(request_timeout_s=0.05, allow_remote=True)
    server.preflight("0.0.0.0", 0)
    status, _, payload = _exchange(server, "/api/storage-health")
    assert status == 200
    assert isinstance(payload, dict)
    server.close(0.1)


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


class _ConcurrencyProbeApplication:
    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.active = 0
        self.maximum_active = 0
        self._lock = threading.Lock()

    def handle(self, request: JsonRequest) -> JsonResponse:
        with self._lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            time.sleep(self.delay_s)
            body: dict[str, Any] = {"path": request.path}
            return JsonResponse(
                200,
                (("content-type", "application/json; charset=utf-8"),),
                canonical_json_bytes(body),
            )
        finally:
            with self._lock:
                self.active -= 1


def test_listener_runs_slow_reads_concurrently_with_a_hard_worker_bound() -> None:
    server = StdlibDashboardServer(
        request_timeout_s=0.01, maximum_concurrent_requests=3
    )
    server.preflight("127.0.0.1", 0)
    application = _ConcurrencyProbeApplication(delay_s=0.08)
    stopped = threading.Event()

    def serve() -> None:
        while not stopped.is_set():
            server.serve_once(application)

    worker = threading.Thread(target=serve)
    worker.start()

    def request(index: int) -> object:
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.bound_port, timeout=2
        )
        connection.request("GET", f"/slow/{index}")
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        assert response.status == 200
        return payload

    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            payloads = list(pool.map(request, range(8)))
    finally:
        stopped.set()
        worker.join(timeout=1)
        server.close(1)

    elapsed = time.monotonic() - started
    assert {payload["path"] for payload in payloads} == {
        f"/slow/{index}" for index in range(8)
    }
    assert application.maximum_active == 3
    assert elapsed < 0.55
    assert not worker.is_alive()


@pytest.mark.parametrize("maximum", [0, 33])
def test_listener_rejects_unsafe_worker_bounds(maximum: int) -> None:
    with pytest.raises(ValueError, match="maximum_concurrent_requests"):
        StdlibDashboardServer(maximum_concurrent_requests=maximum)
