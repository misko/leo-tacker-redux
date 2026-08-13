from __future__ import annotations

import pytest

from leo_flow.analysis.ephemeris.providers import (
    HttpResponse,
    HuggingFaceRetriever,
    ProviderCredentials,
    ProviderResponseError,
    RetryableProviderError,
    SpaceTrackRetriever,
)
from leo_flow.contracts.core import EphemerisRetrievalId
from leo_flow.contracts.ephemeris import EphemerisRetrievalRequest, EphemerisSource

from ._fixtures import MemoryArchive, tle


class Clock:
    def __init__(self) -> None:
        self.value = 1_700_000_000_000_000_000

    def __call__(self) -> int:
        self.value += 1
        return self.value


class Transport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls = []

    def send(self, request, *, credentials=None):
        self.calls.append((request, credentials))
        return self.response


def response(
    body: bytes,
    *,
    status: int = 200,
    headers: tuple[tuple[str, str], ...] | None = None,
) -> HttpResponse:
    return HttpResponse(
        status,
        headers or (("Content-Type", "text/plain"), ("Content-Length", str(len(body)))),
        (body,),
    )


def request(source: EphemerisSource) -> EphemerisRetrievalRequest:
    return EphemerisRetrievalRequest(
        EphemerisRetrievalId("ephret_01"), source, "starlink", "provider-default"
    )


def test_space_track_credentials_are_separate_and_exact_host_only() -> None:
    secret = ProviderCredentials("operator@example.test", "do-not-log-this")
    transport = Transport(response(tle()))
    archive = MemoryArchive()
    retriever = SpaceTrackRetriever(transport, archive, Clock(), secret)
    result = retriever.fetch(request(EphemerisSource.SPACE_TRACK))
    sent, credentials = transport.calls[0]
    assert credentials is secret
    assert sent.url.startswith("https://www.space-track.org/")
    assert sent.allow_redirects is False
    assert secret.username not in repr(sent)
    assert secret.password not in repr(sent)
    assert secret.password not in repr(result)
    with pytest.raises(ValueError, match="restricted"):
        SpaceTrackRetriever(
            transport,
            archive,
            Clock(),
            secret,
            url="https://evil.example/catalog",
        )
    with pytest.raises(ValueError, match="HTTPS"):
        SpaceTrackRetriever(
            transport,
            archive,
            Clock(),
            secret,
            url="https://user:password@www.space-track.org/catalog",
        )


def test_hugging_face_never_receives_space_track_credentials() -> None:
    transport = Transport(response(tle()))
    archive = MemoryArchive()
    result = HuggingFaceRetriever(transport, archive, Clock()).fetch(
        request(EphemerisSource.HUGGING_FACE)
    )
    assert transport.calls[0][1] is None
    assert result.source is EphemerisSource.HUGGING_FACE
    assert archive.sources == [EphemerisSource.HUGGING_FACE]


@pytest.mark.parametrize(
    ("http_response", "message"),
    [
        (response(b"<html>"), "HTML"),
        (
            response(
                b"abc",
                headers=(("Content-Type", "text/plain"), ("Content-Length", "8")),
            ),
            "partial",
        ),
        (response(b"x" * 9), "oversized"),
        (response(b"denied", status=401), "authentication"),
        (response(b"bad", status=500), "HTTP 500"),
    ],
)
def test_invalid_and_bounded_responses_are_not_archived(http_response, message) -> None:
    transport = Transport(http_response)
    archive = MemoryArchive()
    retriever = HuggingFaceRetriever(transport, archive, Clock(), max_response_bytes=8)
    expected = (
        RetryableProviderError if http_response.status >= 500 else ProviderResponseError
    )
    with pytest.raises(expected, match=message):
        retriever.fetch(request(EphemerisSource.HUGGING_FACE))
    assert archive.objects == {}


def test_rate_limit_preserves_retry_after_without_archiving() -> None:
    transport = Transport(
        response(
            b"limited",
            status=429,
            headers=(("Retry-After", "37"),),
        )
    )
    archive = MemoryArchive()
    with pytest.raises(RetryableProviderError) as raised:
        HuggingFaceRetriever(transport, archive, Clock()).fetch(
            request(EphemerisSource.HUGGING_FACE)
        )
    assert raised.value.retry_after_s == 37
    assert archive.objects == {}


@pytest.mark.parametrize(
    "location",
    ("https://evil.example/login", "/relative-login", ""),
)
def test_redirects_never_forward_credentials(location: str) -> None:
    transport = Transport(
        response(
            b"redirect",
            status=302,
            headers=(("Location", location),),
        )
    )
    secret = ProviderCredentials("user", "secret")
    archive = MemoryArchive()
    with pytest.raises(ProviderResponseError, match="redirect"):
        SpaceTrackRetriever(transport, archive, Clock(), secret).fetch(
            request(EphemerisSource.SPACE_TRACK)
        )
    assert len(transport.calls) == 1
    assert archive.objects == {}
