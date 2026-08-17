"""Deterministic JSON request handler without a web-framework commitment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import unquote

from leo_flow.contracts.core import (
    CaptureAttemptId,
    CaptureBatchId,
    DetectorEvaluationId,
    EvaluationRunId,
    RadioId,
    RecordingId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.dashboard import TimeRangeQuery
from leo_flow.contracts.dashboard_batch import (
    CaptureBatchDashboardQueryPortV0_1,
    CaptureBatchTimeRangeQuery,
)
from leo_flow.contracts.dashboard_observation import ObservationAggregateQueryPortV0_1
from leo_flow.contracts.dashboard_recording import (
    RecordingCaptureDetailQueryPortV0_1,
)
from leo_flow.contracts.dashboard_score_distribution import (
    PointScoreDistributionQueryPortV0_2,
    ScoreDistributionQueryPortV0_1,
)
from leo_flow.contracts.dashboard_waterfall import RecordingWaterfallQueryPortV0_1
from leo_flow.contracts.evaluation import DetectorEvaluationView
from leo_flow.contracts.ports import DashboardQueryPort
from leo_flow.contracts.radio_lifecycle import CaptureLifecycleDashboardQueryPortV0_1
from leo_flow.contracts.starlink_pipeline import RecordingStarlinkDecisionQueryPortV0_1
from leo_flow.contracts.starlink_suite_pipeline import (
    RecordingStarlinkSuiteQueryPortV0_2,
)

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


class DashboardJsonApplicationV2:
    """Add versioned batch routes while preserving every dashboard v1 route."""

    _BATCH_ROUTE = "/api/v2/capture-batches"

    def __init__(
        self,
        queries: DashboardQueryPort,
        capture_batches: CaptureBatchDashboardQueryPortV0_1,
    ) -> None:
        self._v1 = DashboardJsonApplication(queries)
        self._capture_batches = capture_batches

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if path != self._BATCH_ROUTE and not path.startswith(f"{self._BATCH_ROUTE}/"):
            return self._v1.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            payload = self._batch_route(path, request.query)
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

    def _batch_route(self, path: str, query: dict[str, str]) -> object:
        if path == self._BATCH_ROUTE:
            return self._capture_batches.recent_capture_batches(
                _capture_batch_time_query(query), query.get("cursor")
            )
        identity = _one_path_component(path, f"{self._BATCH_ROUTE}/")
        return self._capture_batches.capture_batch(CaptureBatchId(identity))


class DashboardJsonApplicationV3:
    """Add projected capture detail and waterfall routes without changing V1/V2."""

    _RECORDING_ROUTE = "/api/v3/recordings"

    def __init__(
        self,
        queries: DashboardQueryPort,
        capture_batches: CaptureBatchDashboardQueryPortV0_1,
        recording_details: RecordingCaptureDetailQueryPortV0_1,
        waterfalls: RecordingWaterfallQueryPortV0_1,
        starlink: RecordingStarlinkDecisionQueryPortV0_1,
    ) -> None:
        self._v2 = DashboardJsonApplicationV2(queries, capture_batches)
        self._recording_details = recording_details
        self._waterfalls = waterfalls
        self._starlink = starlink

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if not path.startswith(f"{self._RECORDING_ROUTE}/"):
            return self._v2.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            payload = self._recording_route(path)
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

    def _recording_route(self, path: str) -> object:
        suffix = path.removeprefix(f"{self._RECORDING_ROUTE}/")
        parts = suffix.split("/")
        if not parts[0] or len(parts) > 2:
            raise DashboardNotFound(f"route {path} was not found")
        recording_id = RecordingId(unquote(parts[0]))
        if len(parts) == 1:
            return self._recording_details.recording_capture_detail(recording_id)
        if parts[1] == "waterfall":
            return self._waterfalls.recording_waterfall(recording_id)
        if parts[1] == "starlink":
            return self._starlink.recording_starlink_decision(recording_id)
        raise DashboardNotFound(f"route {path} was not found")


class DashboardJsonApplicationV4:
    """Expose the complete v0.2 report-method comparison for each recording."""

    _RECORDING_ROUTE = "/api/v4/recordings"

    def __init__(
        self,
        v3: DashboardJsonApplicationV3,
        starlink_suite: RecordingStarlinkSuiteQueryPortV0_2,
    ) -> None:
        self._v3 = v3
        self._starlink_suite = starlink_suite

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if not path.startswith(f"{self._RECORDING_ROUTE}/"):
            return self._v3.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            suffix = path.removeprefix(f"{self._RECORDING_ROUTE}/")
            parts = suffix.split("/")
            if len(parts) != 2 or parts[1] != "starlink-suite":
                raise DashboardNotFound(f"route {path} was not found")
            payload = self._starlink_suite.recording_starlink_suite(
                RecordingId(unquote(parts[0]))
            )
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except DashboardNotFound as error:
            return _error(404, "not_found", str(error))
        except Exception:  # noqa: BLE001
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            canonical_json_bytes(payload),
        )


class DashboardJsonApplicationV5:
    """Add bounded capture-attempt lifecycle detail without changing V1-V4."""

    _PREFIX = "/api/v5/capture-attempts/"

    def __init__(
        self,
        v4: DashboardJsonApplicationV4,
        lifecycle: CaptureLifecycleDashboardQueryPortV0_1,
    ) -> None:
        self._v4 = v4
        self._lifecycle = lifecycle

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if not path.startswith(self._PREFIX):
            return self._v4.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            suffix = path.removeprefix(self._PREFIX)
            parts = suffix.split("/")
            if len(parts) != 2 or parts[1] != "radio-lifecycle":
                raise DashboardNotFound(f"route {path} was not found")
            payload = self._lifecycle.capture_attempt_radio_lifecycle(
                CaptureAttemptId(unquote(parts[0]))
            )
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except DashboardNotFound as error:
            return _error(404, "not_found", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            canonical_json_bytes(payload),
        )


class DashboardJsonApplicationV6:
    """Add truthful RF-duty and candidate-evidence aggregates."""

    _ROUTE = "/api/v6/observation-aggregate"

    def __init__(
        self,
        v5: DashboardJsonApplicationV5,
        aggregates: ObservationAggregateQueryPortV0_1,
    ) -> None:
        self._v5 = v5
        self._aggregates = aggregates

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if path != self._ROUTE:
            return self._v5.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            payload = self._aggregates.observation_aggregate(_time_query(request.query))
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            canonical_json_bytes(payload),
        )


class DashboardJsonApplicationV7:
    """Add bounded detector-score distributions without changing V6."""

    _ROUTE = "/api/v7/score-distributions"

    def __init__(
        self,
        v6: DashboardJsonApplicationV6,
        distributions: ScoreDistributionQueryPortV0_1,
    ) -> None:
        self._v6 = v6
        self._distributions = distributions

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if path != self._ROUTE:
            return self._v6.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            payload = self._distributions.score_distributions(
                _time_query(request.query)
            )
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            canonical_json_bytes(payload),
        )


class DashboardJsonApplicationV8:
    """Expose exact scan-section score points and conditioned controls."""

    _ROUTE = "/api/v8/score-distributions"

    def __init__(
        self,
        v7: DashboardJsonApplicationV7,
        distributions: PointScoreDistributionQueryPortV0_2,
    ) -> None:
        self._v7 = v7
        self._distributions = distributions

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if path != self._ROUTE:
            return self._v7.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            payload = self._distributions.point_score_distributions(
                _time_query(request.query)
            )
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            canonical_json_bytes(payload),
        )


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


def _capture_batch_time_query(query: dict[str, str]) -> CaptureBatchTimeRangeQuery:
    try:
        start = UtcNs(int(query["start_utc_ns"]))
        stop = UtcNs(int(query["stop_utc_ns"]))
    except KeyError as error:
        raise ValueError(f"missing query parameter {error.args[0]}") from error
    except (TypeError, ValueError) as error:
        raise ValueError("UTC bounds must be integers") from error
    return CaptureBatchTimeRangeQuery(start, stop)


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
