from __future__ import annotations

import http.cookiejar
import io

import pytest

from leo_flow.adapters.ephemeris_http import (
    SpaceTrackSessionTransport,
    TransportConfigurationError,
    TransportSessionError,
    UrllibHttpTransport,
)
from leo_flow.analysis.ephemeris.providers import HttpRequest, ProviderCredentials


class Response(io.BytesIO):
    def __init__(self, status: int, body: bytes, headers=()) -> None:
        super().__init__(body)
        self.status = status
        self.headers = dict(headers)

    def getcode(self):
        return self.status


class BrokenReadResponse(Response):
    def read(self, _size: int = -1) -> bytes:
        raise OSError("provider diagnostic must not escape")


class Opener:
    def __init__(self, responses) -> None:
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class TrackingCookieJar(http.cookiejar.CookieJar):
    def __init__(self) -> None:
        super().__init__()
        self.clear_count = 0

    def clear(self, domain=None, path=None, name=None) -> None:
        self.clear_count += 1
        super().clear(domain, path, name)


AUTH_HEADERS = (
    ("Content-Type", "application/json"),
    ("Set-Cookie", "chocolatechip=session-token; Path=/; Secure; HttpOnly"),
)


def test_transport_construction_and_unused_close_perform_no_network_io() -> None:
    opener = Opener(())
    cookies = TrackingCookieJar()
    space_track = SpaceTrackSessionTransport(opener=opener, cookie_jar=cookies)
    hugging_face = UrllibHttpTransport(allowed_hosts=("huggingface.co",), opener=opener)

    assert opener.requests == []
    assert hugging_face is not None
    space_track.close()
    space_track.close()
    assert opener.requests == []
    assert cookies.clear_count == 1


def test_hf_transport_is_host_bounded_and_refuses_credentials() -> None:
    opener = Opener((Response(200, b"tle", (("Content-Type", "text/plain"),)),))
    transport = UrllibHttpTransport(allowed_hosts=("huggingface.co",), opener=opener)
    response = transport.send(HttpRequest("GET", "https://huggingface.co/data"))
    assert b"".join(response.body_chunks) == b"tle"
    with pytest.raises(TransportConfigurationError):
        transport.send(HttpRequest("GET", "https://evil.example/data"))
    with pytest.raises(TransportConfigurationError, match="refuses"):
        transport.send(
            HttpRequest("GET", "https://huggingface.co/data"),
            credentials=ProviderCredentials("user", "secret"),
        )
    with pytest.raises(TransportConfigurationError, match="policy"):
        transport.send(
            HttpRequest(
                "GET",
                "https://huggingface.co/data",
                (("Authorization", "Bearer secret"),),
            )
        )
    assert len(opener.requests) == 1


def test_space_track_logs_in_once_then_reuses_cookie_session() -> None:
    opener = Opener(
        (
            Response(200, b'{"Login":"Success"}', AUTH_HEADERS),
            Response(200, b"tle-one"),
            Response(200, b"tle-two"),
        )
    )
    transport = SpaceTrackSessionTransport(opener=opener)
    credentials = ProviderCredentials("operator@example.test", "secret")
    request = HttpRequest("GET", "https://www.space-track.org/query")
    assert (
        b"".join(transport.send(request, credentials=credentials).body_chunks)
        == b"tle-one"
    )
    assert (
        b"".join(transport.send(request, credentials=credentials).body_chunks)
        == b"tle-two"
    )
    assert len(opener.requests) == 3
    login = opener.requests[0][0]
    assert login.full_url == SpaceTrackSessionTransport.LOGIN_URL
    assert login.get_method() == "POST"
    assert login.data == b""
    assert credentials.password not in repr(login)


@pytest.mark.parametrize(
    ("body", "headers"),
    (
        (
            b"<html><form>Login</form></html>",
            (
                ("Content-Type", "text/html"),
                AUTH_HEADERS[1],
            ),
        ),
        (b'{"Login":"Failed"}', AUTH_HEADERS),
        (b'{"Login":"Success"}', (("Content-Type", "application/json"),)),
        (b"ok", AUTH_HEADERS),
        (b"x" * (64 * 1024 + 1), AUTH_HEADERS),
    ),
)
def test_space_track_http_200_without_authenticated_signal_is_rejected(
    body: bytes, headers: tuple[tuple[str, str], ...]
) -> None:
    opener = Opener((Response(200, body, headers),))
    cookies = TrackingCookieJar()
    transport = SpaceTrackSessionTransport(opener=opener, cookie_jar=cookies)

    response = transport.send(
        HttpRequest("GET", "https://www.space-track.org/query"),
        credentials=ProviderCredentials("operator@example.test", "secret"),
    )

    assert response.status == 401
    assert b"".join(response.body_chunks) == b"authentication rejected"
    assert len(opener.requests) == 1
    assert opener.requests[0][0].data == b""
    assert cookies.clear_count == 1


def test_space_track_close_logs_out_once_clears_session_and_is_final() -> None:
    opener = Opener(
        (
            Response(200, b"", AUTH_HEADERS),
            Response(200, b"tle"),
            Response(204, b""),
        )
    )
    cookies = TrackingCookieJar()
    transport = SpaceTrackSessionTransport(
        opener=opener, cookie_jar=cookies, timeout_s=7.5
    )
    credentials = ProviderCredentials("operator@example.test", "secret")
    request = HttpRequest("GET", "https://www.space-track.org/query")
    assert (
        b"".join(transport.send(request, credentials=credentials).body_chunks) == b"tle"
    )

    transport.close()
    transport.close()

    assert [item[0].full_url for item in opener.requests] == [
        SpaceTrackSessionTransport.LOGIN_URL,
        "https://www.space-track.org/query",
        SpaceTrackSessionTransport.LOGOUT_URL,
    ]
    assert [item[1] for item in opener.requests] == [7.5, 7.5, 7.5]
    assert cookies.clear_count == 1
    with pytest.raises(TransportConfigurationError, match="closed"):
        transport.send(request, credentials=credentials)


@pytest.mark.parametrize(
    "logout_result",
    (
        Response(500, b"failure"),
        OSError("secret detail"),
        BrokenReadResponse(200, b""),
    ),
)
def test_space_track_logout_failure_is_sanitized_and_cleanup_remains_idempotent(
    logout_result: Response | OSError,
) -> None:
    secret = "do-not-expose"
    opener = Opener(
        (
            Response(200, b"", AUTH_HEADERS),
            Response(200, b"tle"),
            logout_result,
        )
    )
    cookies = TrackingCookieJar()
    transport = SpaceTrackSessionTransport(opener=opener, cookie_jar=cookies)
    credentials = ProviderCredentials("operator@example.test", secret)
    request = HttpRequest("GET", "https://www.space-track.org/query")
    assert (
        b"".join(transport.send(request, credentials=credentials).body_chunks) == b"tle"
    )

    with pytest.raises(TransportSessionError) as raised:
        transport.close()
    transport.close()

    assert str(raised.value) == "Space-Track session logout failed"
    assert secret not in str(raised.value)
    assert cookies.clear_count == 1


def test_space_track_login_transport_failure_does_not_retain_or_expose_secret() -> None:
    secret = "do-not-expose"
    opener = Opener((OSError(f"network said {secret}"),))
    transport = SpaceTrackSessionTransport(opener=opener)
    credentials = ProviderCredentials("operator@example.test", secret)

    with pytest.raises(TransportSessionError) as raised:
        transport.send(
            HttpRequest("GET", "https://www.space-track.org/query"),
            credentials=credentials,
        )

    login = opener.requests[0][0]
    assert str(raised.value) == "Space-Track authentication transport failed"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    assert secret not in repr(credentials)
    assert login.data == b""
    assert secret not in repr(login)


def test_space_track_requires_credentials_and_exact_host() -> None:
    transport = SpaceTrackSessionTransport(opener=Opener(()))
    with pytest.raises(TransportConfigurationError, match="required"):
        transport.send(HttpRequest("GET", "https://www.space-track.org/query"))
    with pytest.raises(TransportConfigurationError, match="policy"):
        transport.send(
            HttpRequest("GET", "https://mirror.example/query"),
            credentials=ProviderCredentials("u", "p"),
        )
