"""Bounded, credential-contained provider retrieval adapters."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from leo_flow.contracts.core import UtcNs
from leo_flow.contracts.ephemeris import (
    EphemerisRetrievalRequest,
    EphemerisSource,
    RetrievalResult,
)
from leo_flow.contracts.storage import ObjectRef


class RetrievalError(RuntimeError):
    pass


class ProviderResponseError(RetrievalError):
    def __init__(self, source: EphemerisSource, reason: str) -> None:
        super().__init__(f"{source.value} retrieval failed: {reason}")


class RetryableProviderError(ProviderResponseError):
    def __init__(
        self, source: EphemerisSource, reason: str, retry_after_s: int | None = None
    ) -> None:
        super().__init__(source, reason)
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class ProviderCredentials:
    username: str = field(repr=False)
    password: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.username or not self.password:
            raise ValueError("provider credentials cannot be empty")


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    allow_redirects: bool = False


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body_chunks: Iterable[bytes]


class HttpTransport(Protocol):
    """Transport authenticates without returning or logging credential values."""

    def send(
        self,
        request: HttpRequest,
        *,
        credentials: ProviderCredentials | None = None,
    ) -> HttpResponse: ...


class RawEphemerisArchive(Protocol):
    def put(
        self,
        data: bytes,
        *,
        source: EphemerisSource,
        retrieved_utc_ns: UtcNs,
    ) -> ObjectRef: ...


class _ProviderRetriever:
    def __init__(
        self,
        *,
        source: EphemerisSource,
        exact_url: str,
        transport: HttpTransport,
        archive: RawEphemerisArchive,
        now_utc_ns: Callable[[], int],
        credentials: ProviderCredentials | None = None,
        max_response_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        parsed = urlsplit(exact_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("provider URL must use HTTPS and an exact host")
        if max_response_bytes <= 0:
            raise ValueError("response bound must be positive")
        self._source = source
        self._url = exact_url
        self._host = parsed.hostname
        self._transport = transport
        self._archive = archive
        self._now = now_utc_ns
        self._credentials = credentials
        self._max_bytes = max_response_bytes

    def fetch(self, request: EphemerisRetrievalRequest) -> RetrievalResult:
        if request.source is not self._source:
            raise ValueError("retriever cannot fetch another provider")
        # request_spec is an audit label, never a caller-controlled URL.
        started = UtcNs(self._now())
        http_request = HttpRequest(
            "GET",
            self._url,
            (("Accept", "text/plain,application/octet-stream"),),
        )
        response = self._transport.send(
            http_request,
            credentials=self._credentials,
        )
        body = self._bounded_body(response)
        completed = UtcNs(self._now())
        raw_ref = self._archive.put(
            body,
            source=self._source,
            retrieved_utc_ns=completed,
        )
        return RetrievalResult(
            request.retrieval_id,
            self._source,
            started,
            completed,
            raw_ref,
        )

    def _bounded_body(self, response: HttpResponse) -> bytes:
        try:
            headers = _headers(response.headers)
        except ValueError as error:
            raise ProviderResponseError(self._source, str(error)) from error
        if 300 <= response.status < 400:
            location = headers.get("location", "")
            host = urlsplit(location).hostname if location else None
            if host != self._host:
                raise ProviderResponseError(
                    self._source, "redirect to a different or missing host rejected"
                )
            raise ProviderResponseError(self._source, "redirects are disabled")
        if response.status == 429:
            raise RetryableProviderError(
                self._source,
                "rate limited",
                _retry_after(headers.get("retry-after")),
            )
        if response.status >= 500:
            raise RetryableProviderError(self._source, f"HTTP {response.status}")
        if response.status in (401, 403):
            raise ProviderResponseError(self._source, "authentication rejected")
        if response.status != 200:
            raise ProviderResponseError(self._source, f"HTTP {response.status}")
        content_type = headers.get("content-type", "").lower()
        if "html" in content_type:
            raise ProviderResponseError(self._source, "HTML response rejected")
        try:
            declared = _content_length(headers.get("content-length"))
        except ValueError as error:
            raise ProviderResponseError(self._source, str(error)) from error
        if declared is not None and declared > self._max_bytes:
            raise ProviderResponseError(self._source, "declared response is oversized")
        chunks: list[bytes] = []
        received = 0
        try:
            for chunk in response.body_chunks:
                if not isinstance(chunk, bytes):
                    raise ProviderResponseError(
                        self._source, "transport returned non-bytes"
                    )
                received += len(chunk)
                if received > self._max_bytes:
                    raise ProviderResponseError(self._source, "response is oversized")
                chunks.append(chunk)
        except ProviderResponseError:
            raise
        except OSError as error:
            raise RetryableProviderError(
                self._source, "response body interrupted"
            ) from error
        if declared is not None and received != declared:
            raise ProviderResponseError(self._source, "partial HTTP response")
        body = b"".join(chunks)
        if not body.strip():
            raise ProviderResponseError(self._source, "empty response")
        prefix = body.lstrip()[:32].lower()
        if prefix.startswith((b"<!doctype html", b"<html")):
            raise ProviderResponseError(self._source, "HTML response rejected")
        return body


class SpaceTrackRetriever(_ProviderRetriever):
    DEFAULT_URL = (
        "https://www.space-track.org/basicspacedata/query/"
        "class/gp/OBJECT_NAME/~~STARLINK/decay_date/null-val/epoch/"
        "%3Enow-10/orderby/NORAD_CAT_ID/format/3le"
    )

    def __init__(
        self,
        transport: HttpTransport,
        archive: RawEphemerisArchive,
        now_utc_ns: Callable[[], int],
        credentials: ProviderCredentials,
        *,
        url: str = DEFAULT_URL,
        max_response_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if urlsplit(url).hostname != "www.space-track.org":
            raise ValueError(
                "Space-Track credentials are restricted to www.space-track.org"
            )
        super().__init__(
            source=EphemerisSource.SPACE_TRACK,
            exact_url=url,
            transport=transport,
            archive=archive,
            now_utc_ns=now_utc_ns,
            credentials=credentials,
            max_response_bytes=max_response_bytes,
        )


class HuggingFaceRetriever(_ProviderRetriever):
    DEFAULT_URL = (
        "https://huggingface.co/datasets/juliensimon/starlink-tle-latest/"
        "resolve/main/data/starlink.tle"
    )

    def __init__(
        self,
        transport: HttpTransport,
        archive: RawEphemerisArchive,
        now_utc_ns: Callable[[], int],
        *,
        url: str = DEFAULT_URL,
        max_response_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if urlsplit(url).hostname != "huggingface.co":
            raise ValueError("Hugging Face retriever is restricted to huggingface.co")
        super().__init__(
            source=EphemerisSource.HUGGING_FACE,
            exact_url=url,
            transport=transport,
            archive=archive,
            now_utc_ns=now_utc_ns,
            max_response_bytes=max_response_bytes,
        )


def _headers(headers: tuple[tuple[str, str], ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers:
        lowered = name.lower()
        if lowered in result:
            raise ValueError("duplicate response header")
        result[lowered] = value
    return result


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        result = int(value)
    except ValueError as error:
        raise ValueError("invalid Content-Length") from error
    if result < 0:
        raise ValueError("invalid Content-Length")
    return result


def _retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        result = int(value)
    except ValueError:
        return None
    return result if result >= 0 else None
