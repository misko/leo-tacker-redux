"""Deterministic JSON request handler without a web-framework commitment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import unquote

from leo_flow.contracts.core import RadioId, RecordingId, UtcNs, canonical_json_bytes
from leo_flow.contracts.dashboard import TimeRangeQuery
from leo_flow.contracts.ports import DashboardQueryPort

from .repository import DashboardNotFound, InvalidCursor


@dataclass(frozen=True)
class JsonRequest:
    method: str
    path: str
    query: dict[str, str]


@dataclass(frozen=True)
class JsonResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class JsonDashboardHandler(Protocol):
    def handle(self, request: JsonRequest) -> JsonResponse: ...


class DashboardJsonApplication:
    def __init__(self, queries: DashboardQueryPort) -> None:
        self._queries = queries

    def handle(self, request: JsonRequest) -> JsonResponse:
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            payload = self._route(request)
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except DashboardNotFound as error:
            return _error(404, "not_found", str(error))
        except Exception:  # noqa: BLE001 - deterministic API boundary
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            canonical_json_bytes(payload),
        )

    def _route(self, request: JsonRequest) -> object:
        path = request.path.rstrip("/") or "/"
        if path == "/api/recordings":
            return self._queries.recent_recordings(
                _time_query(request.query), request.query.get("cursor")
            )
        if path == "/api/activity":
            return self._queries.activity(_time_query(request.query))
        if path == "/api/tracks":
            return self._queries.tracks(
                _time_query(request.query), request.query.get("cursor")
            )
        if path == "/api/storage-health":
            return self._queries.storage_health()
        if path.startswith("/api/models/"):
            identity = _one_path_component(path, "/api/models/")
            return self._queries.model_snapshot(identity)
        if path.startswith("/api/recordings/"):
            suffix = path.removeprefix("/api/recordings/")
            parts = suffix.split("/")
            recording_id = RecordingId(unquote(parts[0]))
            if len(parts) == 1:
                return self._queries.recording_detail(recording_id)
            if len(parts) == 2 and parts[1] == "features":
                selector = request.query.get("selector")
                if selector is None:
                    raise ValueError("selector is required")
                return self._queries.recording_features(
                    recording_id, selector, request.query.get("cursor")
                )
        raise DashboardNotFound(f"route {path} was not found")


def _time_query(query: dict[str, str]) -> TimeRangeQuery:
    try:
        start = UtcNs(int(query["start_utc_ns"]))
        stop = UtcNs(int(query["stop_utc_ns"]))
    except KeyError as error:
        raise ValueError(f"missing query parameter {error.args[0]}") from error
    except (TypeError, ValueError) as error:
        raise ValueError("UTC bounds must be integers") from error
    radio_text = query.get("radio_ids", "")
    radios = tuple(RadioId(item) for item in radio_text.split(",") if item)
    if len(radios) != len(set(radios)):
        raise ValueError("radio_ids must be unique")
    return TimeRangeQuery(start, stop, radios)


def _one_path_component(path: str, prefix: str) -> str:
    value = unquote(path.removeprefix(prefix))
    if not value or "/" in value:
        raise DashboardNotFound(f"route {path} was not found")
    return value


def _error(status: int, code: str, message: str) -> JsonResponse:
    return JsonResponse(
        status,
        (("content-type", "application/json; charset=utf-8"),),
        canonical_json_bytes({"error": {"code": code, "message": message}}),
    )
