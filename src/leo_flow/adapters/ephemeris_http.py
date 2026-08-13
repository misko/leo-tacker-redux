"""Small stdlib HTTP adapters for ephemeris provider ports.

The analysis component remains network-free.  Tests inject an opener and never
contact a provider.  Redirects are rejected so Space-Track credentials cannot
cross an authority boundary.
"""

from __future__ import annotations

import http.cookiejar
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from leo_flow.analysis.ephemeris.providers import (
    HttpRequest,
    HttpResponse,
    ProviderCredentials,
)


class TransportConfigurationError(RuntimeError):
    pass


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def _default_opener() -> Any:
    return urllib.request.build_opener(_RejectRedirects())


class UrllibHttpTransport:
    """Unauthenticated HTTPS transport restricted to configured hosts."""

    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...],
        timeout_s: float = 30.0,
        opener: Any | None = None,
    ) -> None:
        if not allowed_hosts or timeout_s <= 0:
            raise ValueError("allowed hosts and a positive timeout are required")
        self._hosts = frozenset(allowed_hosts)
        self._timeout = timeout_s
        self._opener = opener or _default_opener()

    def send(
        self,
        request: HttpRequest,
        *,
        credentials: ProviderCredentials | None = None,
    ) -> HttpResponse:
        if credentials is not None:
            raise TransportConfigurationError(
                "unauthenticated transport refuses provider credentials"
            )
        _validate_request(request, self._hosts)
        return _open(self._opener, request, timeout_s=self._timeout)


class SpaceTrackSessionTransport:
    """Cookie-session adapter for the documented Space-Track login flow."""

    LOGIN_URL = "https://www.space-track.org/ajaxauth/login"

    def __init__(self, *, timeout_s: float = 30.0, opener: Any | None = None) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout must be positive")
        self._timeout = timeout_s
        self._opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            _RejectRedirects(),
        )
        self._authenticated = False
        self._lock = threading.RLock()

    def send(
        self,
        request: HttpRequest,
        *,
        credentials: ProviderCredentials | None = None,
    ) -> HttpResponse:
        if credentials is None:
            raise TransportConfigurationError("Space-Track credentials are required")
        _validate_request(request, frozenset(("www.space-track.org",)))
        with self._lock:
            if not self._authenticated:
                status = self._login(credentials)
                if status not in (200, 204):
                    return HttpResponse(status, (), (b"authentication rejected",))
                self._authenticated = True
            return _open(self._opener, request, timeout_s=self._timeout)

    def _login(self, credentials: ProviderCredentials) -> int:
        data = urllib.parse.urlencode(
            {"identity": credentials.username, "password": credentials.password}
        ).encode("ascii")
        login = urllib.request.Request(
            self.LOGIN_URL,
            data=data,
            headers={
                "Accept": "application/json,text/plain",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            response = self._opener.open(login, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            error.close()
            return int(error.code)
        with response:
            # Consume a bounded diagnostic response so the connection is reusable.
            response.read(64 * 1024 + 1)
            return int(response.getcode())


def _validate_request(request: HttpRequest, allowed_hosts: frozenset[str]) -> None:
    parsed = urlsplit(request.url)
    if (
        request.method != "GET"
        or parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or request.allow_redirects
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise TransportConfigurationError("HTTP request violates transport policy")


def _open(opener: Any, request: HttpRequest, *, timeout_s: float) -> HttpResponse:
    native = urllib.request.Request(
        request.url,
        headers=dict(request.headers),
        method=request.method,
    )
    try:
        response = opener.open(native, timeout=timeout_s)
    except urllib.error.HTTPError as error:
        response = error
    status = int(response.getcode())
    headers = tuple((str(key), str(value)) for key, value in response.headers.items())
    return HttpResponse(status, headers, _chunks(response))


def _chunks(response: Any) -> Iterable[bytes]:
    with response:
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                return
            yield chunk
