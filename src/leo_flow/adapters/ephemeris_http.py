"""Small stdlib HTTP adapters for ephemeris provider ports.

The analysis component remains network-free.  Tests inject an opener and never
contact a provider.  Redirects are rejected so Space-Track credentials cannot
cross an authority boundary.
"""

from __future__ import annotations

import http.cookiejar
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import urlsplit

from leo_flow.analysis.ephemeris.providers import (
    HttpRequest,
    HttpResponse,
    ProviderCredentials,
)


class TransportConfigurationError(RuntimeError):
    pass


class TransportSessionError(RuntimeError):
    """A bounded provider-session operation failed without exposing details."""


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
    LOGOUT_URL = "https://www.space-track.org/ajaxauth/logout"
    _MAX_AUTH_RESPONSE_BYTES = 64 * 1024

    def __init__(
        self,
        *,
        timeout_s: float = 30.0,
        opener: Any | None = None,
        cookie_jar: http.cookiejar.CookieJar | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout must be positive")
        self._timeout = timeout_s
        self._cookies = http.cookiejar.CookieJar() if cookie_jar is None else cookie_jar
        self._opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookies), _RejectRedirects()
        )
        self._authenticated = False
        self._closed = False
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
            if self._closed:
                raise TransportConfigurationError("Space-Track session is closed")
            if not self._authenticated:
                if not self._login(credentials):
                    self._clear_session()
                    return HttpResponse(401, (), (b"authentication rejected",))
                self._authenticated = True
            return _open(self._opener, request, timeout_s=self._timeout)

    def close(self) -> None:
        """Bound logout once and clear all locally owned session state.

        Cleanup is final and idempotent.  A logout failure is reported only as a
        stable error category; cookies and authentication state are still
        cleared before the error leaves this boundary.
        """

        with self._lock:
            if self._closed:
                return
            self._closed = True
            logout_error = False
            try:
                if self._authenticated:
                    logout_error = not self._logout()
            finally:
                self._clear_session()
            if logout_error:
                raise TransportSessionError("Space-Track session logout failed")

    def _login(self, credentials: ProviderCredentials) -> bool:
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
            try:
                response = self._opener.open(login, timeout=self._timeout)
            except urllib.error.HTTPError as error:
                error.close()
                return False
            except OSError:
                raise TransportSessionError(
                    "Space-Track authentication transport failed"
                ) from None
            try:
                with response:
                    status = int(response.getcode())
                    body = response.read(self._MAX_AUTH_RESPONSE_BYTES + 1)
                    if len(body) > self._MAX_AUTH_RESPONSE_BYTES:
                        return False
                    return _authenticated_login_response(status, response.headers, body)
            except OSError:
                raise TransportSessionError(
                    "Space-Track authentication transport failed"
                ) from None
        finally:
            # urllib has already transmitted the request before ``open``
            # returns. Do not retain an encoded credential copy on the Request
            # object in injected openers, tracebacks, or test diagnostics.
            login.data = b""

    def _logout(self) -> bool:
        logout = urllib.request.Request(
            self.LOGOUT_URL,
            headers={"Accept": "application/json,text/plain"},
            method="GET",
        )
        try:
            response = self._opener.open(logout, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            error.close()
            return False
        except OSError:
            return False
        try:
            with response:
                body = response.read(self._MAX_AUTH_RESPONSE_BYTES + 1)
                return int(response.getcode()) in (200, 204) and len(body) <= (
                    self._MAX_AUTH_RESPONSE_BYTES
                )
        except OSError:
            return False

    def _clear_session(self) -> None:
        self._authenticated = False
        self._cookies.clear()


def _authenticated_login_response(status: int, headers: Any, body: bytes) -> bool:
    """Accept only a bounded, non-HTML response that establishes a cookie."""

    if status not in (200, 204) or not _has_space_track_session_cookie(headers):
        return False
    content_type = _first_header(headers, "content-type").casefold()
    stripped = body.strip()
    if "html" in content_type or stripped[:32].lower().startswith(
        (b"<!doctype html", b"<html")
    ):
        return False
    if not stripped:
        return True
    try:
        value = json.loads(stripped)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    login = value.get("Login", value.get("login"))
    return isinstance(login, str) and login.casefold() == "success"


def _has_space_track_session_cookie(headers: Any) -> bool:
    values = _header_values(headers, "set-cookie")
    for value in values:
        parsed = SimpleCookie()
        try:
            parsed.load(value)
        except CookieError:
            continue
        for morsel in parsed.values():
            domain = morsel["domain"].lstrip(".").casefold()
            if (
                morsel.value
                and morsel["secure"]
                and (not domain or domain in {"space-track.org", "www.space-track.org"})
            ):
                return True
    return False


def _first_header(headers: Any, name: str) -> str:
    values = _header_values(headers, name)
    return "" if not values else values[0]


def _header_values(headers: Any, name: str) -> tuple[str, ...]:
    getter = getattr(headers, "get_all", None)
    if callable(getter):
        values = getter(name, [])
        return tuple(str(value) for value in values)
    items = getattr(headers, "items", None)
    if callable(items):
        return tuple(
            str(value)
            for key, value in items()
            if str(key).casefold() == name.casefold()
        )
    return ()


def _validate_request(request: HttpRequest, allowed_hosts: frozenset[str]) -> None:
    parsed = urlsplit(request.url)
    sensitive_headers = {"authorization", "cookie", "proxy-authorization"}
    if (
        request.method != "GET"
        or parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or request.allow_redirects
        or parsed.username is not None
        or parsed.password is not None
        or any(name.casefold() in sensitive_headers for name, _ in request.headers)
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
