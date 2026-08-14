"""Deterministic JSON request handler without a web-framework commitment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import unquote

from leo_flow.contracts.core import (
    DetectorEvaluationId,
    EvaluationRunId,
    RadioId,
    RecordingId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.dashboard import TimeRangeQuery
from leo_flow.contracts.evaluation import DetectorEvaluationView
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
        if path.startswith("/api/evaluations/"):
            identity, identity_kind = _evaluation_identity(
                _one_path_component(path, "/api/evaluations/")
            )
            return _evaluation_payload(
                self._queries.detector_evaluation(identity),
                queried_identity=identity,
                queried_identity_kind=identity_kind,
            )
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


def _evaluation_identity(value: str) -> tuple[str, str]:
    if value.startswith("eval_"):
        return str(DetectorEvaluationId(value)), "evaluation_id"
    if value.startswith("erun_"):
        return str(EvaluationRunId(value)), "run_id"
    raise ValueError("evaluation identity must start with 'eval_' or 'erun_'")


def _evaluation_payload(
    view: DetectorEvaluationView,
    *,
    queried_identity: str,
    queried_identity_kind: str,
) -> dict[str, object]:
    report = view.ref.report_object
    expected_locator = f"cas:{report.digest.algorithm.value}:{report.digest.value}"
    if report.locator != expected_locator:
        raise RuntimeError("evaluation report locator is not a canonical CAS locator")
    return {
        "schema_version": 1,
        "queried_identity": queried_identity,
        "queried_identity_kind": queried_identity_kind,
        "evaluation_id": str(view.ref.evaluation_id),
        "run_id": str(view.ref.run_id),
        "method_count": view.method_count,
        "union_window_count": view.union_window_count,
        "warnings": view.warnings,
        "methods": tuple(
            {
                "method_id": method.method_id,
                "split": method.split,
                "threshold": method.threshold,
                "score_semantics": method.score_semantics,
                "coverage": {
                    "feature_set_count": method.feature_set_count,
                    "feature_set_present_count": method.feature_set_present_count,
                    "union_window_count": method.union_window_count,
                    "present_window_count": method.present_window_count,
                    "missing_window_count": method.missing_window_count,
                    "scored_prediction_count": method.scored_prediction_count,
                    "missing_prediction_count": method.missing_prediction_count,
                },
                "firing_count": method.firing_count,
                "confusion": {
                    "true_positive": method.true_positive,
                    "false_positive": method.false_positive,
                    "true_negative": method.true_negative,
                    "false_negative": method.false_negative,
                },
            }
            for method in view.methods
        ),
        "report_object": report,
    }


def _error(status: int, code: str, message: str) -> JsonResponse:
    return JsonResponse(
        status,
        (("content-type", "application/json; charset=utf-8"),),
        canonical_json_bytes({"error": {"code": code, "message": message}}),
    )
