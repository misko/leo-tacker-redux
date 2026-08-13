"""Bounded stdlib HTTP transport for the read-only dashboard JSON API."""

from __future__ import annotations

import ipaddress
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Final
from urllib.parse import parse_qsl, urlsplit

from leo_flow.contracts.core import canonical_json_bytes
from leo_flow.dashboard.api import JsonDashboardHandler, JsonRequest, JsonResponse

_MAX_QUERY_FIELDS: Final = 64


class DashboardHttpError(RuntimeError):
    """The dashboard HTTP listener cannot be used safely."""


class StdlibDashboardServer:
    """Single-request-at-a-time HTTP adapter with a finite idle wait.

    The default listener policy permits loopback only. A distinct deployment
    adapter must make any future authenticated/TLS remote exposure explicit.
    """

    def __init__(self, *, request_timeout_s: float = 0.25) -> None:
        if request_timeout_s <= 0:
            raise ValueError("request_timeout_s must be positive")
        self._request_timeout_s = request_timeout_s
        self._server: HTTPServer | None = None
        self._handler: JsonDashboardHandler | None = None
        self._handled = threading.Event()

    @property
    def bound_port(self) -> int:
        if self._server is None:
            raise DashboardHttpError("dashboard server is not bound")
        return int(self._server.server_address[1])

    def preflight(self, bind_host: str, bind_port: int) -> None:
        if self._server is not None:
            return
        if not _is_loopback(bind_host):
            raise DashboardHttpError("dashboard v1 listener requires a loopback host")
        if not 0 <= bind_port <= 65535:
            raise DashboardHttpError("dashboard bind port is invalid")
        try:
            server = HTTPServer((bind_host, bind_port), self._request_handler())
        except OSError as error:
            raise DashboardHttpError("dashboard listener bind failed") from error
        server.timeout = self._request_timeout_s
        self._server = server

    def serve_once(self, handler: JsonDashboardHandler) -> bool:
        if self._server is None:
            raise DashboardHttpError("dashboard server has not passed preflight")
        self._handler = handler
        self._handled.clear()
        self._server.handle_request()
        return self._handled.is_set()

    def close(self, timeout_s: float) -> None:
        if timeout_s <= 0:
            raise ValueError("close timeout must be positive")
        server, self._server = self._server, None
        self._handler = None
        if server is not None:
            server.server_close()

    def _request_handler(self) -> type[BaseHTTPRequestHandler]:
        adapter = self

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
                adapter._handled.set()
                response = self._response()
                self.send_response(response.status)
                for name, value in response.headers:
                    if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
                        raise DashboardHttpError("dashboard response header is invalid")
                    self.send_header(name, value)
                self.send_header("content-length", str(len(response.body)))
                self.send_header("cache-control", "no-store")
                self.send_header("x-content-type-options", "nosniff")
                self.send_header("connection", "close")
                self.end_headers()
                if write_body:
                    self.wfile.write(response.body)
                self.close_connection = True

            def _response(self) -> JsonResponse:
                handler = adapter._handler
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
