"""Same-origin static operator UI composed over the stable JSON application."""

from __future__ import annotations

from importlib.resources import files
from types import MappingProxyType
from typing import Final
from urllib.parse import unquote

from leo_flow.contracts.core import RecordingId

from .api import JsonDashboardHandler, JsonRequest, JsonResponse

_CONTENT_SECURITY_POLICY: Final = (
    "default-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; "
    + "script-src 'self'; style-src 'self'; base-uri 'none'; "
    + "form-action 'none'; frame-ancestors 'none'"
)
_SECURITY_HEADERS: Final = (
    (
        "content-security-policy",
        _CONTENT_SECURITY_POLICY,
    ),
    ("referrer-policy", "no-referrer"),
    ("x-frame-options", "DENY"),
    ("cross-origin-resource-policy", "same-origin"),
    ("permissions-policy", "camera=(), microphone=(), geolocation=()"),
)
_RETRO_QAM_SOURCE_PATH: Final = "/canaries/retro-qam/source-recording"
_RETRO_QAM_SOURCE_URL: Final = "/recordings/rec_retro_qam_20260813_clip002"
_RETRO_QAM_REPORT_PATH: Final = "/canaries/retro-qam/report"
_RETRO_QAM_REPORT_URL: Final = (
    "https://github.com/misko/leo-tracker-redux/blob/main/"
    "reports/qam_retro_investigation.md"
)


class DashboardUiApplication:
    """Serve a fixed asset allow-list and delegate every API request unchanged."""

    def __init__(self, api: JsonDashboardHandler) -> None:
        package = files(__package__) / "static"
        self._api = api
        self._routes = MappingProxyType(
            {
                "/": (
                    "text/html; charset=utf-8",
                    "no-store",
                    (package / "index.html").read_bytes(),
                ),
                "/assets/dashboard.css": (
                    "text/css; charset=utf-8",
                    "public, max-age=300",
                    (package / "dashboard.css").read_bytes(),
                ),
                "/assets/dashboard.js": (
                    "text/javascript; charset=utf-8",
                    "public, max-age=300",
                    (package / "dashboard.js").read_bytes(),
                ),
                "/canaries/retro-qam": (
                    "text/html; charset=utf-8",
                    "no-store",
                    (package / "retro-qam-canary.html").read_bytes(),
                ),
                "/assets/retro-qam-canary.js": (
                    "text/javascript; charset=utf-8",
                    "public, max-age=300",
                    (package / "retro-qam-canary.js").read_bytes(),
                ),
                "/assets/recording-detail.js": (
                    "text/javascript; charset=utf-8",
                    "public, max-age=300",
                    (package / "recording-detail.js").read_bytes(),
                ),
                "/assets/recording-evidence.js": (
                    "text/javascript; charset=utf-8",
                    "public, max-age=300",
                    (package / "recording-evidence.js").read_bytes(),
                ),
                "/assets/symbolwise-replay.js": (
                    "text/javascript; charset=utf-8",
                    "public, max-age=300",
                    (package / "symbolwise-replay.js").read_bytes(),
                ),
                "/assets/receiver-agnostic-cfo-qam.js": (
                    "text/javascript; charset=utf-8",
                    "public, max-age=300",
                    (package / "receiver-agnostic-cfo-qam.js").read_bytes(),
                ),
                "/aggregate-stats": (
                    "text/html; charset=utf-8",
                    "no-store",
                    (package / "aggregate-stats.html").read_bytes(),
                ),
                "/assets/aggregate-stats.js": (
                    "text/javascript; charset=utf-8",
                    "public, max-age=300",
                    (package / "aggregate-stats.js").read_bytes(),
                ),
                "/aggregate-doppler": (
                    "text/html; charset=utf-8",
                    "no-store",
                    (package / "aggregate-doppler.html").read_bytes(),
                ),
                "/assets/aggregate-doppler.js": (
                    "text/javascript; charset=utf-8",
                    "public, max-age=300",
                    (package / "aggregate-doppler.js").read_bytes(),
                ),
                "/full-dwell": (
                    "text/html; charset=utf-8",
                    "no-store",
                    (package / "full-dwell.html").read_bytes(),
                ),
                "/assets/full-dwell.js": (
                    "text/javascript; charset=utf-8",
                    "public, max-age=300",
                    (package / "full-dwell.js").read_bytes(),
                ),
            }
        )
        self._recording_page = (
            "text/html; charset=utf-8",
            "no-store",
            (package / "recording.html").read_bytes(),
        )

    def handle(self, request: JsonRequest) -> JsonResponse:
        redirects = {
            _RETRO_QAM_SOURCE_PATH: _RETRO_QAM_SOURCE_URL,
            _RETRO_QAM_REPORT_PATH: _RETRO_QAM_REPORT_URL,
        }
        if request.path in redirects and request.method.upper() in {
            "GET",
            "HEAD",
        }:
            return JsonResponse(
                302,
                (
                    ("location", redirects[request.path]),
                    ("cache-control", "no-store"),
                    *_SECURITY_HEADERS,
                ),
                b"",
            )
        route = self._routes.get(request.path)
        if route is None and _recording_page_id(request.path) is not None:
            route = self._recording_page
        if route is None:
            if request.path.startswith("/assets/"):
                return JsonResponse(
                    404,
                    (
                        ("content-type", "text/plain; charset=utf-8"),
                        ("cache-control", "no-store"),
                        *_SECURITY_HEADERS,
                    ),
                    b"Not found\n",
                )
            return self._api.handle(request)
        if request.method.upper() not in {"GET", "HEAD"}:
            return self._api.handle(request)
        content_type, cache_control, body = route
        return JsonResponse(
            200,
            (
                ("content-type", content_type),
                ("cache-control", cache_control),
                *_SECURITY_HEADERS,
            ),
            body,
        )


def _recording_page_id(path: str) -> RecordingId | None:
    prefix = "/recordings/"
    if not path.startswith(prefix):
        return None
    identity = unquote(path.removeprefix(prefix))
    if not identity or "/" in identity:
        return None
    try:
        return RecordingId(identity)
    except ValueError:
        return None
