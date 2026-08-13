from __future__ import annotations

import io

import pytest

from leo_flow.adapters.ephemeris_http import (
    SpaceTrackSessionTransport,
    TransportConfigurationError,
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


class Opener:
    def __init__(self, responses) -> None:
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        return next(self.responses)


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


def test_space_track_logs_in_once_then_reuses_cookie_session() -> None:
    opener = Opener(
        (
            Response(200, b"ok"),
            Response(200, b"tle-one"),
            Response(200, b"tle-two"),
        )
    )
    transport = SpaceTrackSessionTransport(opener=opener)
    credentials = ProviderCredentials("operator@example.test", "secret")
    request = HttpRequest("GET", "https://www.space-track.org/query")
    assert b"".join(transport.send(request, credentials=credentials).body_chunks) == b"tle-one"
    assert b"".join(transport.send(request, credentials=credentials).body_chunks) == b"tle-two"
    assert len(opener.requests) == 3
    login = opener.requests[0][0]
    assert login.full_url == SpaceTrackSessionTransport.LOGIN_URL
    assert login.get_method() == "POST"
    assert credentials.password not in repr(login)


def test_space_track_requires_credentials_and_exact_host() -> None:
    transport = SpaceTrackSessionTransport(opener=Opener(()))
    with pytest.raises(TransportConfigurationError, match="required"):
        transport.send(HttpRequest("GET", "https://www.space-track.org/query"))
    with pytest.raises(TransportConfigurationError, match="policy"):
        transport.send(
            HttpRequest("GET", "https://mirror.example/query"),
            credentials=ProviderCredentials("u", "p"),
        )
