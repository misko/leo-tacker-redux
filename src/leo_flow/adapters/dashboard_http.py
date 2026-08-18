"""Bounded stdlib HTTP transport for the read-only dashboard JSON API."""

from __future__ import annotations

import ipaddress
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from socket import socket
from socketserver import ThreadingMixIn
from typing import Final
from urllib.parse import parse_qsl, urlsplit

from leo_flow.contracts.core import canonical_json_bytes
from leo_flow.dashboard.api import JsonDashboardHandler, JsonRequest, JsonResponse

_MAX_QUERY_FIELDS: Final = 64
_DEFAULT_MAXIMUM_CONCURRENT_REQUESTS: Final = 4
_MAXIMUM_CONCURRENT_REQUESTS: Final = 32
_SocketRequest = socket | tuple[bytes, socket]


class DashboardHttpError(RuntimeError):
    """The dashboard HTTP listener cannot be used safely."""


class _BoundedThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Thread-per-request transport with a hard worker and handler bound."""

    daemon_threads = True
    block_on_close = False

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler: type[BaseHTTPRequestHandler],
        *,
        maximum_concurrent_requests: int,
        handler_snapshot: Callable[[], JsonDashboardHandler | None],
        request_accepted: threading.Event,
    ) -> None:
        self._request_slots = threading.BoundedSemaphore(maximum_concurrent_requests)
        self._snapshot = handler_snapshot
        self._request_accepted = request_accepted
        self._snapshots: dict[_SocketRequest, JsonDashboardHandler | None] = {}
        self._snapshots_lock = threading.Lock()
        super().__init__(server_address, request_handler)

    def process_request(self, request: _SocketRequest, client_address: object) -> None:
        self._request_slots.acquire()
        with self._snapshots_lock:
            self._snapshots[request] = self._snapshot()
        self._request_accepted.set()
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._snapshots_lock:
                self._snapshots.pop(request, None)
            self._request_slots.release()
            raise

    def process_request_thread(
        self, request: _SocketRequest, client_address: object
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._snapshots_lock:
                self._snapshots.pop(request, None)
            self._request_slots.release()

    def dashboard_handler(
        self, request: _SocketRequest
    ) -> JsonDashboardHandler | None:
        with self._snapshots_lock:
            return self._snapshots.get(request)


class StdlibDashboardServer:
    """Bounded-concurrency HTTP adapter with a finite idle wait.

    The default listener policy permits loopback only. A distinct deployment
    adapter must make remote exposure explicit; authentication and TLS remain
    deployment-network responsibilities.
    """

    def __init__(
        self,
        *,
        request_timeout_s: float = 0.25,
        allow_remote: bool = False,
        maximum_concurrent_requests: int = _DEFAULT_MAXIMUM_CONCURRENT_REQUESTS,
    ) -> None:
        if request_timeout_s <= 0:
            raise ValueError("request_timeout_s must be positive")
        if not 1 <= maximum_concurrent_requests <= _MAXIMUM_CONCURRENT_REQUESTS:
            raise ValueError(
                "maximum_concurrent_requests must be between "
                f"1 and {_MAXIMUM_CONCURRENT_REQUESTS}"
            )
        self._request_timeout_s = request_timeout_s
        self._allow_remote = allow_remote
        self._maximum_concurrent_requests = maximum_concurrent_requests
        self._server: _BoundedThreadingHTTPServer | None = None
        self._handler: JsonDashboardHandler | None = None
        self._handler_lock = threading.Lock()
        self._handled = threading.Event()

    @property
    def bound_port(self) -> int:
        if self._server is None:
            raise DashboardHttpError("dashboard server is not bound")
        return int(self._server.server_address[1])

    def preflight(self, bind_host: str, bind_port: int) -> None:
        if self._server is not None:
            return
        if not self._allow_remote and not _is_loopback(bind_host):
            raise DashboardHttpError("dashboard v1 listener requires a loopback host")
        if not 0 <= bind_port <= 65535:
            raise DashboardHttpError("dashboard bind port is invalid")
        try:
            server = _BoundedThreadingHTTPServer(
                (bind_host, bind_port),
                self._request_handler(),
                maximum_concurrent_requests=self._maximum_concurrent_requests,
                handler_snapshot=self._handler_snapshot,
                request_accepted=self._handled,
            )
        except OSError as error:
            raise DashboardHttpError("dashboard listener bind failed") from error
        server.timeout = self._request_timeout_s
        self._server = server

    def serve_once(self, handler: JsonDashboardHandler) -> bool:
        if self._server is None:
            raise DashboardHttpError("dashboard server has not passed preflight")
        with self._handler_lock:
            self._handler = handler
        self._handled.clear()
        self._server.handle_request()
        return self._handled.is_set()

    def close(self, timeout_s: float) -> None:
        if timeout_s <= 0:
            raise ValueError("close timeout must be positive")
        server, self._server = self._server, None
        if server is not None:
            server.server_close()
        with self._handler_lock:
            self._handler = None

    def _handler_snapshot(self) -> JsonDashboardHandler | None:
        with self._handler_lock:
            return self._handler

    def _request_handler(self) -> type[BaseHTTPRequestHandler]:
        class RequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "leo-flow-dashboard-v1"
            sys_version = ""

            def do_GET(self) -> None:
                self._dispatch()

            def do_HEAD(self) -> None:
                self._dispatch(write_body=False)

            def do_POST(self) -> None:
                self._dispatch()

            def do_PUT(self) -> None:
                self._dispatch()

            def do_PATCH(self) -> None:
                self._dispatch()

            def do_DELETE(self) -> None:
                self._dispatch()

            def do_OPTIONS(self) -> None:
                self._dispatch()

            def log_message(self, format: str, *args: object) -> None:
                del format, args

            def _dispatch(self, *, write_body: bool = True) -> None:
                response = self._response()
                self.send_response(response.status)
                for name, value in response.headers:
                    if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
                        raise DashboardHttpError("dashboard response header is invalid")
                    self.send_header(name, value)
                self.send_header("content-length", str(len(response.body)))
                header_names = {name.casefold() for name, _ in response.headers}
                if "cache-control" not in header_names:
                    self.send_header("cache-control", "no-store")
                if "x-content-type-options" not in header_names:
                    self.send_header("x-content-type-options", "nosniff")
                self.send_header("connection", "close")
                self.end_headers()
                if write_body:
                    self.wfile.write(response.body)
                self.close_connection = True

            def _response(self) -> JsonResponse:
                server = self.server
                if not isinstance(server, _BoundedThreadingHTTPServer):
                    return _transport_error(503, "dashboard handler is unavailable")
                handler = server.dashboard_handler(self.request)
                if handler is None:
                    return _transport_error(503, "dashboard handler is unavailable")
                try:
                    parsed = urlsplit(self.path)
                    pairs = parse_qsl(
                        parsed.query,
                        keep_blank_values=True,
                        max_num_fields=_MAX_QUERY_FIELDS,
                    )
                except ValueError:
                    return _transport_error(400, "query string is invalid")
                query: dict[str, str] = {}
                for name, value in pairs:
                    if name in query:
                        return _transport_error(400, "query parameters must be unique")
                    query[name] = value
                return handler.handle(JsonRequest(self.command, parsed.path, query))

        return RequestHandler


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _transport_error(status: int, message: str) -> JsonResponse:
    return JsonResponse(
        status,
        (("content-type", "application/json; charset=utf-8"),),
        canonical_json_bytes(
            {"error": {"code": "invalid_http_request", "message": message}}
        ),
    )
