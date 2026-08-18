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
    ReceiverChainId,
    RecordingId,
    SegmentId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.dashboard import TimeRangeQuery
from leo_flow.contracts.dashboard_advanced_doppler import (
    RecordingEvidenceAdvancedDopplerQueryPortV0_1,
)
from leo_flow.contracts.dashboard_batch import (
    CaptureBatchDashboardQueryPortV0_1,
    CaptureBatchTimeRangeQuery,
)
from leo_flow.contracts.dashboard_capture_doppler import (
    MAX_CAPTURE_DOPPLER_RECORDINGS,
    CaptureDopplerSummaryQueryPortV0_1,
    CaptureDopplerSummaryQueryV0_1,
)
from leo_flow.contracts.dashboard_capture_qam import (
    MAX_CAPTURE_QAM_RECORDINGS,
    CaptureQamSummaryQueryPortV0_1,
    CaptureQamSummaryQueryV0_1,
)
from leo_flow.contracts.dashboard_doppler import (
    DopplerWaterfallLayer,
    RecordingDopplerVisualizationQueryPortV0_1,
)
from leo_flow.contracts.dashboard_doppler_aggregate import (
    DopplerAggregateQueryPortV0_1,
    DopplerAggregateQueryV0_1,
)
from leo_flow.contracts.dashboard_full_dwell_timeline import (
    MAXIMUM_FULL_DWELL_TIMELINE_WINDOWS,
    FullDwellTimelineQueryV0_1,
    RecordingFullDwellTimelineQueryPortV0_1,
)
from leo_flow.contracts.dashboard_observation import ObservationAggregateQueryPortV0_1
from leo_flow.contracts.dashboard_pilot_doppler import (
    PilotDopplerAssociationQueryV0_1,
    RecordingPilotDopplerAssociationQueryPortV0_1,
)
from leo_flow.contracts.dashboard_recording import (
    RecordingCaptureDetailQueryPortV0_1,
)
from leo_flow.contracts.dashboard_recording_analysis_approach import (
    RecordingAnalysisApproachQueryPortV0_1,
)
from leo_flow.contracts.dashboard_recording_evidence import (
    MAXIMUM_DOPPLER_WINDOW_ESTIMATES,
    RecordingEvidenceContextQueryPortV0_1,
    RecordingEvidenceDopplerQueryPortV0_1,
    RecordingEvidenceDopplerQueryV0_1,
)
from leo_flow.contracts.dashboard_retro_qam_canary import (
    RetroQamCanaryDashboardQueryPortV0_1,
)
from leo_flow.contracts.dashboard_score_distribution import (
    PointScoreDistributionQueryPortV0_2,
    ScoreDistributionQueryPortV0_1,
)
from leo_flow.contracts.dashboard_surrogate_distribution import (
    SurrogateScoreDistributionQueryPortV0_1,
)
from leo_flow.contracts.dashboard_temporal_pilot import (
    TemporalPilotAggregateQueryPortV0_1,
)
from leo_flow.contracts.dashboard_waterfall import RecordingWaterfallQueryPortV0_1
from leo_flow.contracts.evaluation import DetectorEvaluationView
from leo_flow.contracts.ports import DashboardQueryPort
from leo_flow.contracts.radio_lifecycle import CaptureLifecycleDashboardQueryPortV0_1
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_acquired_constellation_pipeline import (
    RecordingStarlinkAcquiredConstellationQueryPortV0_3,
    StarlinkAcquiredConstellationQueryV0_3,
    StarlinkAcquiredConstellationViewMode,
)
from leo_flow.contracts.starlink_adaptive_qam import (
    RecordingStarlinkAdaptiveQamQueryPortV0_4,
)
from leo_flow.contracts.starlink_adaptive_response import (
    RecordingStarlinkAdaptiveResponseQueryPortV0_1,
    StarlinkAdaptiveResponseQueryV0_1,
)
from leo_flow.contracts.starlink_detector_suite import (
    REPORT_METHOD_ORDER,
    StarlinkDetectorMethod,
)
from leo_flow.contracts.starlink_full_dwell_response import (
    MAXIMUM_FULL_DWELL_QUERY_POINTS,
    RecordingStarlinkFullDwellQueryPortV0_1,
    StarlinkFullDwellQueryV0_1,
)
from leo_flow.contracts.starlink_pilot_constellation import MAX_CONSTELLATION_POINTS
from leo_flow.contracts.starlink_pilot_constellation_pipeline import (
    MAX_CONSTELLATION_QUERY_STREAMS,
    RecordingStarlinkPilotConstellationQueryPortV0_1,
    StarlinkPilotConstellationQueryV0_1,
)
from leo_flow.contracts.starlink_pilot_prescreen import (
    RecordingStarlinkPilotPrescreenQueryPortV0_1,
    StarlinkPilotPrescreenQueryV0_1,
)
from leo_flow.contracts.starlink_pipeline import RecordingStarlinkDecisionQueryPortV0_1
from leo_flow.contracts.starlink_suite_pipeline import (
    RecordingStarlinkSuiteQueryPortV0_2,
)
from leo_flow.contracts.starlink_surrogate_null_pipeline import (
    MAXIMUM_SURROGATE_NULL_QUERY_ROWS,
    RecordingStarlinkSurrogateNullQueryPortV0_1,
    StarlinkSurrogateNullQueryV0_1,
)
from leo_flow.contracts.starlink_temporal_pilot import (
    MAXIMUM_TEMPORAL_QUERY_POINTS,
    RecordingStarlinkTemporalPilotQueryPortV0_1,
    StarlinkTemporalPilotQueryV0_1,
)

from .repository import DashboardNotFound, InvalidCursor

_MAX_SURROGATE_QUERY_TEXT_BYTES = 8_192
_MAX_SURROGATE_RADIO_FILTERS = 64
_MAX_CONSTELLATION_QUERY_TEXT_BYTES = 8_192
_MAX_CONSTELLATION_SEGMENT_FILTERS = 64
_MAX_CONSTELLATION_RECEIVER_FILTERS = 16
_MAX_CONSTELLATION_RESPONSE_BYTES = 16 * 1_024 * 1_024


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


class DashboardJsonApplicationV9:
    """Add a bounded waterfall v0.2 and blind-Doppler presentation route."""

    _PREFIX = "/api/v9/recordings/"

    def __init__(
        self,
        v8: DashboardJsonApplicationV8,
        visualizations: RecordingDopplerVisualizationQueryPortV0_1,
    ) -> None:
        self._v8 = v8
        self._visualizations = visualizations

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if not path.startswith(self._PREFIX):
            return self._v8.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            suffix = path.removeprefix(self._PREFIX)
            parts = suffix.split("/")
            if len(parts) != 2 or parts[1] != "doppler-visualization":
                raise DashboardNotFound(f"route {path} was not found")
            payload = self._visualizations.recording_doppler_visualization(
                RecordingId(unquote(parts[0])),
                DopplerWaterfallLayer(
                    request.query.get("layer", DopplerWaterfallLayer.RESIDUAL.value)
                ),
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


class DashboardJsonApplicationV10:
    """Add bounded paired-surrogate evidence without changing V1--V9 routes."""

    _PREFIX = "/api/v10/recordings/"

    def __init__(
        self,
        v9: DashboardJsonApplicationV9,
        surrogate_nulls: RecordingStarlinkSurrogateNullQueryPortV0_1,
    ) -> None:
        self._v9 = v9
        self._surrogate_nulls = surrogate_nulls

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if not path.startswith(self._PREFIX):
            return self._v9.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            suffix = path.removeprefix(self._PREFIX)
            parts = suffix.split("/")
            if len(parts) != 2 or parts[1] != "starlink-surrogate-null":
                raise DashboardNotFound(f"route {path} was not found")
            query = _starlink_surrogate_null_query(
                RecordingId(unquote(parts[0])), request.query
            )
            payload = self._surrogate_nulls.recording_starlink_surrogate_null(query)
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


class DashboardJsonApplicationV11:
    """Add bounded published-pilot constellation evidence without changing V1--V10."""

    _PREFIX = "/api/v11/recordings/"

    def __init__(
        self,
        v10: DashboardJsonApplicationV10,
        constellations: RecordingStarlinkPilotConstellationQueryPortV0_1,
    ) -> None:
        self._v10 = v10
        self._constellations = constellations

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if not path.startswith(self._PREFIX):
            return self._v10.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            suffix = path.removeprefix(self._PREFIX)
            parts = suffix.split("/")
            if len(parts) != 2 or parts[1] != "starlink-pilot-constellation":
                raise DashboardNotFound(f"route {path} was not found")
            query = _starlink_pilot_constellation_query(
                RecordingId(unquote(parts[0])), request.query
            )
            payload = self._constellations.recording_starlink_pilot_constellation(query)
            encoded = canonical_json_bytes(payload)
            if len(encoded) > _MAX_CONSTELLATION_RESPONSE_BYTES:
                raise RuntimeError("constellation response exceeds its byte bound")
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except DashboardNotFound as error:
            return _error(404, "not_found", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            encoded,
        )


class DashboardJsonApplicationV12:
    """Add bounded aggregate Qin-versus-surrogate distributions."""

    _ROUTE = "/api/v12/surrogate-score-distributions"

    def __init__(
        self,
        v11: DashboardJsonApplicationV11,
        distributions: SurrogateScoreDistributionQueryPortV0_1,
    ) -> None:
        self._v11 = v11
        self._distributions = distributions

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if path != self._ROUTE:
            return self._v11.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            payload = self._distributions.surrogate_score_distributions(
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


class DashboardJsonApplicationV13:
    """Add bounded stratified temporal Qin-versus-surrogate traces."""

    _PREFIX = "/api/v13/recordings/"

    def __init__(
        self,
        v12: DashboardJsonApplicationV12,
        temporal: RecordingStarlinkTemporalPilotQueryPortV0_1,
        aggregate: TemporalPilotAggregateQueryPortV0_1 | None = None,
    ) -> None:
        self._v12, self._temporal, self._aggregate = v12, temporal, aggregate

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if path == "/api/v13/temporal-pilot-aggregate":
            if request.method.upper() != "GET":
                return _error(405, "method_not_allowed", "only GET is supported")
            if self._aggregate is None:
                return _error(404, "not_found", "temporal aggregate is unavailable")
            try:
                aggregate_payload = self._aggregate.temporal_pilot_aggregate(
                    _time_query(request.query)
                )
            except (ValueError, InvalidCursor) as error:
                return _error(400, "invalid_request", str(error))
            except Exception:  # noqa: BLE001 - fixed external error contract
                return _error(500, "internal_error", "dashboard query failed")
            return JsonResponse(
                200,
                (("content-type", "application/json; charset=utf-8"),),
                canonical_json_bytes(aggregate_payload),
            )
        if not path.startswith(self._PREFIX):
            return self._v12.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            suffix = path.removeprefix(self._PREFIX)
            parts = suffix.split("/")
            if len(parts) != 2 or parts[1] != "starlink-temporal-pilot":
                raise DashboardNotFound(f"route {path} was not found")
            query = _starlink_temporal_query(
                RecordingId(unquote(parts[0])), request.query
            )
            payload = self._temporal.recording_starlink_temporal_pilot(query)
            encoded = canonical_json_bytes(payload)
            if len(encoded) > 32 * 1024 * 1024:
                raise RuntimeError("temporal response exceeds its byte bound")
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except DashboardNotFound as error:
            return _error(404, "not_found", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            encoded,
        )


class DashboardJsonApplicationV14:
    """Add bounded candidate-only aggregate Doppler evidence."""

    _ROUTE = "/api/v14/doppler-aggregate"
    _MAX_QUERY_BYTES = 16_384
    _MAX_RESPONSE_BYTES = 16 * 1024 * 1024

    def __init__(
        self,
        v13: DashboardJsonApplicationV13,
        aggregate: DopplerAggregateQueryPortV0_1,
    ) -> None:
        self._v13, self._aggregate = v13, aggregate

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if path != self._ROUTE:
            return self._v13.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            payload = self._aggregate.doppler_aggregate(
                _doppler_aggregate_query(request.query, self._MAX_QUERY_BYTES)
            )
            encoded = canonical_json_bytes(payload)
            if len(encoded) > self._MAX_RESPONSE_BYTES:
                raise RuntimeError("Doppler aggregate response exceeds its byte bound")
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            encoded,
        )


class DashboardJsonApplicationV15:
    """Add the v0.1 sparse-exact full-dwell product without changing V1--V14."""

    _PREFIX = "/api/v15/recordings/"
    _MAX_QUERY_BYTES = 16_384
    _MAX_RESPONSE_BYTES = 32 * 1024 * 1024

    def __init__(
        self,
        v14: DashboardJsonApplicationV14,
        full_dwell: RecordingStarlinkFullDwellQueryPortV0_1,
    ) -> None:
        self._v14, self._full_dwell = v14, full_dwell

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if not path.startswith(self._PREFIX):
            return self._v14.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            suffix = path.removeprefix(self._PREFIX)
            parts = suffix.split("/")
            if len(parts) != 2 or parts[1] != "starlink-full-dwell":
                raise DashboardNotFound(f"route {path} was not found")
            query = _starlink_full_dwell_query(
                RecordingId(unquote(parts[0])), request.query, self._MAX_QUERY_BYTES
            )
            payload = self._full_dwell.recording_starlink_full_dwell(query)
            encoded = canonical_json_bytes(payload)
            if len(encoded) > self._MAX_RESPONSE_BYTES:
                raise RuntimeError("full-dwell response exceeds its byte bound")
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except DashboardNotFound as error:
            return _error(404, "not_found", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            encoded,
        )


class DashboardJsonApplicationV16:
    """Add authoritative selector context for the unified evidence workspace."""

    _PREFIX = "/api/v16/recordings/"
    _MAX_RESPONSE_BYTES = 256 * 1024

    def __init__(
        self,
        v15: DashboardJsonApplicationV15,
        evidence_contexts: RecordingEvidenceContextQueryPortV0_1,
        evidence_doppler: RecordingEvidenceDopplerQueryPortV0_1,
    ) -> None:
        self._v15 = v15
        self._evidence_contexts = evidence_contexts
        self._evidence_doppler = evidence_doppler

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if not path.startswith(self._PREFIX):
            return self._v15.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            suffix = path.removeprefix(self._PREFIX)
            parts = suffix.split("/")
            if len(parts) != 2:
                raise DashboardNotFound(f"route {path} was not found")
            recording_id = RecordingId(unquote(parts[0]))
            if parts[1] == "evidence-context":
                if request.query:
                    raise ValueError(
                        "evidence-context does not accept query parameters"
                    )
                encoded = canonical_json_bytes(
                    self._evidence_contexts.recording_evidence_context(recording_id)
                )
            elif parts[1] == "evidence-doppler":
                encoded = canonical_json_bytes(
                    self._evidence_doppler.recording_evidence_doppler(
                        _recording_evidence_doppler_query(recording_id, request.query)
                    )
                )
            else:
                raise DashboardNotFound(f"route {path} was not found")
            if len(encoded) > self._MAX_RESPONSE_BYTES:
                raise RuntimeError("recording-evidence context exceeds its byte bound")
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except DashboardNotFound as error:
            return _error(404, "not_found", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            encoded,
        )


class DashboardJsonApplicationV17:
    """Add bounded windowed/overall acquired-QAM without changing V1--V16."""

    _PREFIX = "/api/v17/recordings/"
    _MAX_QUERY_BYTES = 16_384
    _MAX_RESPONSE_BYTES = 32 * 1024 * 1024

    def __init__(
        self,
        v16: DashboardJsonApplicationV16,
        acquired_qam: RecordingStarlinkAcquiredConstellationQueryPortV0_3,
    ) -> None:
        self._v16, self._acquired_qam = v16, acquired_qam

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if not path.startswith(self._PREFIX):
            return self._v16.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            suffix = path.removeprefix(self._PREFIX)
            parts = suffix.split("/")
            if len(parts) != 2 or parts[1] != "starlink-acquired-constellation":
                raise DashboardNotFound(f"route {path} was not found")
            payload = self._acquired_qam.recording_starlink_acquired_constellation(
                _starlink_acquired_constellation_query(
                    RecordingId(unquote(parts[0])),
                    request.query,
                    self._MAX_QUERY_BYTES,
                )
            )
            encoded = canonical_json_bytes(payload)
            if len(encoded) > self._MAX_RESPONSE_BYTES:
                raise RuntimeError("acquired-QAM response exceeds its byte bound")
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except DashboardNotFound as error:
            return _error(404, "not_found", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            encoded,
        )


class DashboardJsonApplicationV18:
    """Add one bounded bulk Doppler summary for the master capture table."""

    _ROUTE = "/api/v18/capture-doppler-summaries"
    _MAX_RESPONSE_BYTES = 4 * 1024 * 1024

    def __init__(
        self,
        previous: JsonDashboardHandler,
        summaries: CaptureDopplerSummaryQueryPortV0_1,
    ) -> None:
        self._previous = previous
        self._summaries = summaries

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if path != self._ROUTE:
            return self._previous.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            payload = self._summaries.capture_doppler_summaries(
                _capture_doppler_summary_query(request.query)
            )
            encoded = canonical_json_bytes(payload)
            if len(encoded) > self._MAX_RESPONSE_BYTES:
                raise RuntimeError("capture Doppler summary exceeds its byte bound")
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except DashboardNotFound as error:
            return _error(404, "not_found", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            encoded,
        )


class DashboardJsonApplicationV19:
    """Add exact total/window estimates for advanced-path-only Doppler evidence."""

    _PREFIX = "/api/v19/recordings/"
    _MAX_RESPONSE_BYTES = 4 * 1024 * 1024

    def __init__(
        self,
        previous: JsonDashboardHandler,
        advanced_doppler: RecordingEvidenceAdvancedDopplerQueryPortV0_1,
    ) -> None:
        self._previous = previous
        self._advanced_doppler = advanced_doppler

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if not path.startswith(self._PREFIX):
            return self._previous.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            suffix = path.removeprefix(self._PREFIX)
            parts = suffix.split("/")
            if len(parts) != 2 or parts[1] != "evidence-advanced-doppler":
                raise DashboardNotFound(f"route {path} was not found")
            payload = self._advanced_doppler.recording_evidence_advanced_doppler(
                _recording_evidence_doppler_query(
                    RecordingId(unquote(parts[0])), request.query
                )
            )
            encoded = canonical_json_bytes(payload)
            if len(encoded) > self._MAX_RESPONSE_BYTES:
                raise RuntimeError("advanced Doppler evidence exceeds its byte bound")
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except DashboardNotFound as error:
            return _error(404, "not_found", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            encoded,
        )


class DashboardJsonApplicationV20:
    """Expose the complete cheap full-dwell timeline without changing V15."""

    _PREFIX = "/api/v20/recordings/"
    _MAX_QUERY_BYTES = 8_192
    _MAX_RESPONSE_BYTES = 16 * 1024 * 1024

    def __init__(
        self,
        previous: JsonDashboardHandler,
        timelines: RecordingFullDwellTimelineQueryPortV0_1,
    ) -> None:
        self._previous, self._timelines = previous, timelines

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if not path.startswith(self._PREFIX):
            return self._previous.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            suffix = path.removeprefix(self._PREFIX)
            parts = suffix.split("/")
            if len(parts) != 2 or parts[1] != "full-dwell-timeline":
                raise DashboardNotFound(f"route {path} was not found")
            payload = self._timelines.recording_full_dwell_timeline(
                _full_dwell_timeline_query(
                    RecordingId(unquote(parts[0])), request.query, self._MAX_QUERY_BYTES
                )
            )
            encoded = canonical_json_bytes(payload)
            if len(encoded) > self._MAX_RESPONSE_BYTES:
                raise RuntimeError("full-dwell timeline exceeds its byte bound")
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except DashboardNotFound as error:
            return _error(404, "not_found", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            encoded,
        )


class DashboardJsonApplicationV21:
    """Expose the latest immutable historical-QAM acceptance receipt."""

    _PATH = "/api/v21/canaries/retro-qam/latest"
    _MAX_RESPONSE_BYTES = 64 * 1024

    def __init__(
        self,
        previous: JsonDashboardHandler,
        canary: RetroQamCanaryDashboardQueryPortV0_1,
    ) -> None:
        self._previous, self._canary = previous, canary

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if path != self._PATH:
            return self._previous.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            if request.query:
                raise ValueError("retro-QAM canary query does not accept parameters")
            encoded = canonical_json_bytes(self._canary.latest_retro_qam_canary())
            if len(encoded) > self._MAX_RESPONSE_BYTES:
                raise RuntimeError("retro-QAM canary response exceeds its byte bound")
        except ValueError as error:
            return _error(400, "invalid_request", str(error))
        except LookupError as error:
            return _error(404, "not_found", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            encoded,
        )


class DashboardJsonApplicationV22:
    """Add one bounded QAM-goodness summary for the master capture table."""

    _ROUTE = "/api/v22/capture-qam-summaries"
    _MAX_RESPONSE_BYTES = 4 * 1024 * 1024

    def __init__(
        self,
        previous: JsonDashboardHandler,
        summaries: CaptureQamSummaryQueryPortV0_1,
    ) -> None:
        self._previous, self._summaries = previous, summaries

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if path != self._ROUTE:
            return self._previous.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            payload = self._summaries.capture_qam_summaries(
                _capture_qam_summary_query(request.query)
            )
            encoded = canonical_json_bytes(payload)
            if len(encoded) > self._MAX_RESPONSE_BYTES:
                raise RuntimeError("capture QAM summary exceeds its byte bound")
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except DashboardNotFound as error:
            return _error(404, "not_found", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            encoded,
        )


class DashboardJsonApplicationV23:
    """Expose exact persisted analysis/search/windowing facts per recording."""

    _PREFIX = "/api/v23/recordings/"
    _MAX_RESPONSE_BYTES = 256 * 1024

    def __init__(
        self,
        previous: JsonDashboardHandler,
        approaches: RecordingAnalysisApproachQueryPortV0_1,
    ) -> None:
        self._previous, self._approaches = previous, approaches

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if not path.startswith(self._PREFIX):
            return self._previous.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            suffix = path.removeprefix(self._PREFIX)
            parts = suffix.split("/")
            if len(parts) != 2 or parts[1] != "analysis-approaches":
                raise DashboardNotFound(f"route {path} was not found")
            if request.query:
                raise ValueError("analysis-approaches does not accept query parameters")
            payload = self._approaches.recording_analysis_approach(
                RecordingId(unquote(parts[0]))
            )
            encoded = canonical_json_bytes(payload)
            if len(encoded) > self._MAX_RESPONSE_BYTES:
                raise RuntimeError(
                    "analysis-approaches response exceeds its byte bound"
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
            encoded,
        )


class DashboardJsonApplicationV24:
    """Expose pattern-symmetric adaptive detector responses across a dwell."""

    _PREFIX = "/api/v24/recordings/"
    _MAX_QUERY_BYTES = 8_192
    _MAX_RESPONSE_BYTES = 32 * 1024 * 1024

    def __init__(
        self,
        previous: JsonDashboardHandler,
        responses: RecordingStarlinkAdaptiveResponseQueryPortV0_1,
    ) -> None:
        self._previous, self._responses = previous, responses

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if not path.startswith(self._PREFIX):
            return self._previous.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            suffix = path.removeprefix(self._PREFIX)
            parts = suffix.split("/")
            if len(parts) != 2 or parts[1] != "starlink-adaptive-response":
                raise DashboardNotFound(f"route {path} was not found")
            payload = self._responses.recording_starlink_adaptive_response(
                _starlink_adaptive_response_query(
                    RecordingId(unquote(parts[0])),
                    request.query,
                    self._MAX_QUERY_BYTES,
                )
            )
            encoded = canonical_json_bytes(payload)
            if len(encoded) > self._MAX_RESPONSE_BYTES:
                raise RuntimeError("adaptive response exceeds its byte bound")
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except DashboardNotFound as error:
            return _error(404, "not_found", str(error))
        except LookupError as error:
            return _error(404, "not_found", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            encoded,
        )


class DashboardJsonApplicationV25:
    """Expose adaptively selected acquired-QAM windows without changing V17."""

    _PREFIX = "/api/v25/recordings/"
    _MAX_QUERY_BYTES = 16_384
    _MAX_RESPONSE_BYTES = 32 * 1024 * 1024

    def __init__(
        self,
        previous: JsonDashboardHandler,
        adaptive_qam: RecordingStarlinkAdaptiveQamQueryPortV0_4,
    ) -> None:
        self._previous, self._adaptive_qam = previous, adaptive_qam

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if not path.startswith(self._PREFIX):
            return self._previous.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            suffix = path.removeprefix(self._PREFIX)
            parts = suffix.split("/")
            if len(parts) != 2 or parts[1] != "starlink-adaptive-qam":
                raise DashboardNotFound(f"route {path} was not found")
            payload = self._adaptive_qam.recording_starlink_adaptive_qam(
                _starlink_acquired_constellation_query(
                    RecordingId(unquote(parts[0])),
                    request.query,
                    self._MAX_QUERY_BYTES,
                )
            )
            encoded = canonical_json_bytes(payload)
            if len(encoded) > self._MAX_RESPONSE_BYTES:
                raise RuntimeError("adaptive-QAM response exceeds its byte bound")
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except (DashboardNotFound, LookupError) as error:
            return _error(404, "not_found", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            encoded,
        )


class DashboardJsonApplicationV26:
    """Compare acquired-pilot frequency tracks with blind Doppler paths."""

    _PREFIX = "/api/v26/recordings/"
    _MAX_QUERY_BYTES = 8_192
    _MAX_RESPONSE_BYTES = 8 * 1024 * 1024

    def __init__(
        self,
        previous: JsonDashboardHandler,
        associations: RecordingPilotDopplerAssociationQueryPortV0_1,
    ) -> None:
        self._previous, self._associations = previous, associations

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if not path.startswith(self._PREFIX):
            return self._previous.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            suffix = path.removeprefix(self._PREFIX)
            parts = suffix.split("/")
            if len(parts) != 2 or parts[1] != "pilot-doppler-association":
                raise DashboardNotFound(f"route {path} was not found")
            payload = self._associations.recording_pilot_doppler_association(
                _pilot_doppler_association_query(
                    RecordingId(unquote(parts[0])),
                    request.query,
                    self._MAX_QUERY_BYTES,
                )
            )
            encoded = canonical_json_bytes(payload)
            if len(encoded) > self._MAX_RESPONSE_BYTES:
                raise RuntimeError("pilot Doppler association exceeds its byte bound")
        except (ValueError, InvalidCursor) as error:
            return _error(400, "invalid_request", str(error))
        except (DashboardNotFound, LookupError) as error:
            return _error(404, "not_found", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            encoded,
        )


class DashboardJsonApplicationV27:
    """Expose complete-IQ pattern-blind OFDM pilot-prescreen timelines."""

    _PREFIX = "/api/v27/recordings/"
    _MAX_QUERY_BYTES = 8_192
    _MAX_RESPONSE_BYTES = 8 * 1024 * 1024

    def __init__(
        self,
        previous: JsonDashboardHandler,
        prescreens: RecordingStarlinkPilotPrescreenQueryPortV0_1,
    ) -> None:
        self._previous, self._prescreens = previous, prescreens

    def handle(self, request: JsonRequest) -> JsonResponse:
        path = request.path.rstrip("/") or "/"
        if not path.startswith(self._PREFIX):
            return self._previous.handle(request)
        if request.method.upper() != "GET":
            return _error(405, "method_not_allowed", "only GET is supported")
        try:
            suffix = path.removeprefix(self._PREFIX)
            parts = suffix.split("/")
            if len(parts) != 2 or parts[1] != "starlink-pilot-prescreen":
                raise DashboardNotFound(f"route {path} was not found")
            payload = self._prescreens.recording_starlink_pilot_prescreen(
                _pilot_prescreen_query(
                    RecordingId(unquote(parts[0])),
                    request.query,
                    self._MAX_QUERY_BYTES,
                )
            )
            encoded = canonical_json_bytes(payload)
            if len(encoded) > self._MAX_RESPONSE_BYTES:
                raise RuntimeError("pilot-prescreen response exceeds its byte bound")
        except ValueError as error:
            return _error(400, "invalid_request", str(error))
        except (DashboardNotFound, LookupError) as error:
            return _error(404, "not_found", str(error))
        except Exception:  # noqa: BLE001 - fixed external error contract
            return _error(500, "internal_error", "dashboard query failed")
        return JsonResponse(
            200,
            (("content-type", "application/json; charset=utf-8"),),
            encoded,
        )


def _pilot_prescreen_query(
    recording_id: RecordingId,
    query: dict[str, str],
    maximum_query_bytes: int,
) -> StarlinkPilotPrescreenQueryV0_1:
    allowed = {
        "radio_ids",
        "lnb_ids",
        "receiver_chain_ids",
        "edges",
        "maximum_points",
    }
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameter {unknown[0]}")
    if sum(len(key) + len(value) for key, value in query.items()) > maximum_query_bytes:
        raise ValueError("pilot-prescreen query is too large")
    try:
        return StarlinkPilotPrescreenQueryV0_1(
            recording_id,
            tuple(
                RadioId(value)
                for value in (_comma_values(query, "radio_ids", 64) or ())
            ),
            _comma_values(query, "lnb_ids", 64) or (),
            tuple(
                ReceiverChainId(value)
                for value in (_comma_values(query, "receiver_chain_ids", 32) or ())
            ),
            tuple(
                StarlinkEdge(value)
                for value in (_comma_values(query, "edges", 2) or ())
            ),
            int(query.get("maximum_points", "4096")),
        )
    except ValueError as error:
        raise ValueError("invalid pilot-prescreen query value") from error


def _pilot_doppler_association_query(
    recording_id: RecordingId,
    query: dict[str, str],
    maximum_query_bytes: int,
) -> PilotDopplerAssociationQueryV0_1:
    allowed = {
        "radio_ids",
        "lnb_ids",
        "receiver_chain_ids",
        "edges",
        "maximum_windows_per_stream",
    }
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameter {unknown[0]}")
    if sum(len(key) + len(value) for key, value in query.items()) > maximum_query_bytes:
        raise ValueError("pilot Doppler query is too large")
    return PilotDopplerAssociationQueryV0_1(
        recording_id,
        tuple(RadioId(value) for value in (_comma_values(query, "radio_ids", 2) or ())),
        _comma_values(query, "lnb_ids", 16) or (),
        tuple(
            ReceiverChainId(value)
            for value in (_comma_values(query, "receiver_chain_ids", 16) or ())
        ),
        tuple(
            StarlinkEdge(value) for value in (_comma_values(query, "edges", 2) or ())
        ),
        _optional_positive_int(query, "maximum_windows_per_stream", 32),
    )


def _starlink_adaptive_response_query(
    recording_id: RecordingId,
    query: dict[str, str],
    maximum_query_bytes: int,
) -> StarlinkAdaptiveResponseQueryV0_1:
    allowed = {
        "methods",
        "radio_ids",
        "lnb_ids",
        "receiver_chain_ids",
        "edges",
        "maximum_points",
    }
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameter {unknown[0]}")
    if (
        sum(len(key.encode()) + len(value.encode()) for key, value in query.items())
        > maximum_query_bytes
    ):
        raise ValueError("adaptive response query text exceeds its bound")
    methods_raw = _comma_values(query, "methods", len(REPORT_METHOD_ORDER))
    try:
        return StarlinkAdaptiveResponseQueryV0_1(
            recording_id,
            REPORT_METHOD_ORDER
            if methods_raw is None
            else tuple(StarlinkDetectorMethod(value) for value in methods_raw),
            tuple(
                RadioId(value)
                for value in (_comma_values(query, "radio_ids", 64) or ())
            ),
            _comma_values(query, "lnb_ids", 64) or (),
            tuple(
                ReceiverChainId(value)
                for value in (_comma_values(query, "receiver_chain_ids", 32) or ())
            ),
            tuple(
                StarlinkEdge(value)
                for value in (_comma_values(query, "edges", 2) or ())
            ),
            int(query.get("maximum_points", "4096")),
        )
    except ValueError as error:
        raise ValueError("invalid adaptive response query value") from error


def _capture_qam_summary_query(
    query: dict[str, str],
) -> CaptureQamSummaryQueryV0_1:
    allowed = {"start_utc_ns", "stop_utc_ns", "maximum_recordings"}
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameter {unknown[0]}")
    try:
        return CaptureQamSummaryQueryV0_1(
            UtcNs(int(query["start_utc_ns"])),
            UtcNs(int(query["stop_utc_ns"])),
            int(query.get("maximum_recordings", str(MAX_CAPTURE_QAM_RECORDINGS))),
        )
    except KeyError as error:
        raise ValueError(f"missing query parameter {error.args[0]}") from error
    except ValueError as error:
        raise ValueError("capture QAM summary query is invalid") from error


def _capture_doppler_summary_query(
    query: dict[str, str],
) -> CaptureDopplerSummaryQueryV0_1:
    allowed = {"start_utc_ns", "stop_utc_ns", "maximum_recordings"}
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameter {unknown[0]}")
    try:
        return CaptureDopplerSummaryQueryV0_1(
            UtcNs(int(query["start_utc_ns"])),
            UtcNs(int(query["stop_utc_ns"])),
            int(query.get("maximum_recordings", str(MAX_CAPTURE_DOPPLER_RECORDINGS))),
        )
    except KeyError as error:
        raise ValueError(f"missing query parameter {error.args[0]}") from error
    except ValueError as error:
        raise ValueError("invalid capture Doppler summary query") from error


def _recording_evidence_doppler_query(
    recording_id: RecordingId, query: dict[str, str]
) -> RecordingEvidenceDopplerQueryV0_1:
    allowed = {"radio_ids", "lnb_ids", "receiver_chain_ids", "maximum_windows"}
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameter {unknown[0]}")
    radios = _comma_values(query, "radio_ids", 2) or ()
    lnbs = _comma_values(query, "lnb_ids", 16) or ()
    receivers = _comma_values(query, "receiver_chain_ids", 16) or ()
    return RecordingEvidenceDopplerQueryV0_1(
        recording_id,
        tuple(RadioId(value) for value in radios),
        lnbs,
        tuple(ReceiverChainId(value) for value in receivers),
        _optional_positive_int(
            query, "maximum_windows", MAXIMUM_DOPPLER_WINDOW_ESTIMATES
        ),
    )


def _starlink_acquired_constellation_query(
    recording_id: RecordingId,
    query: dict[str, str],
    maximum_query_bytes: int,
) -> StarlinkAcquiredConstellationQueryV0_3:
    allowed = {
        "mode",
        "radio_ids",
        "lnb_ids",
        "segment_ids",
        "receiver_chain_ids",
        "edges",
        "maximum_streams",
        "maximum_windows_per_stream",
        "maximum_points_per_constellation",
    }
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameter {unknown[0]}")
    if (
        sum(len(key.encode()) + len(value.encode()) for key, value in query.items())
        > maximum_query_bytes
    ):
        raise ValueError("acquired-QAM query text exceeds its bound")
    try:
        return StarlinkAcquiredConstellationQueryV0_3(
            recording_id,
            StarlinkAcquiredConstellationViewMode(query.get("mode", "overall")),
            tuple(
                RadioId(value)
                for value in (_comma_values(query, "radio_ids", 64) or ())
            ),
            _comma_values(query, "lnb_ids", 64) or (),
            tuple(
                SegmentId(value)
                for value in (_comma_values(query, "segment_ids", 64) or ())
            ),
            tuple(
                ReceiverChainId(value)
                for value in (_comma_values(query, "receiver_chain_ids", 32) or ())
            ),
            tuple(
                StarlinkEdge(value)
                for value in (_comma_values(query, "edges", 2) or ())
            ),
            int(query.get("maximum_streams", "4")),
            int(query.get("maximum_windows_per_stream", "8")),
            int(query.get("maximum_points_per_constellation", "1200")),
        )
    except ValueError as error:
        raise ValueError("invalid acquired-QAM query value") from error


def _starlink_full_dwell_query(
    recording_id: RecordingId, query: dict[str, str], maximum_query_bytes: int
) -> StarlinkFullDwellQueryV0_1:
    allowed = {"methods", "radio_ids", "receiver_chain_ids", "edges", "maximum_points"}
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameter {unknown[0]}")
    if (
        sum(len(k.encode()) + len(v.encode()) for k, v in query.items())
        > maximum_query_bytes
    ):
        raise ValueError("full-dwell query text exceeds its bound")
    methods_raw = _comma_values(query, "methods", len(REPORT_METHOD_ORDER))
    methods = (
        REPORT_METHOD_ORDER
        if methods_raw is None
        else tuple(StarlinkDetectorMethod(value) for value in methods_raw)
    )
    radios_raw = _comma_values(query, "radio_ids", 64) or ()
    receivers_raw = _comma_values(query, "receiver_chain_ids", 32) or ()
    edges_raw = _comma_values(query, "edges", 2) or ()
    try:
        maximum_points = int(query.get("maximum_points", "1024"))
    except ValueError as error:
        raise ValueError("maximum_points must be an integer") from error
    if maximum_points > MAXIMUM_FULL_DWELL_QUERY_POINTS:
        raise ValueError("maximum_points exceeds its bound")
    return StarlinkFullDwellQueryV0_1(
        recording_id,
        methods,
        tuple(RadioId(v) for v in radios_raw),
        tuple(ReceiverChainId(v) for v in receivers_raw),
        tuple(StarlinkEdge(v) for v in edges_raw),
        maximum_points,
    )


def _full_dwell_timeline_query(
    recording_id: RecordingId, query: dict[str, str], maximum_query_bytes: int
) -> FullDwellTimelineQueryV0_1:
    allowed = {"radio_ids", "receiver_chain_ids", "edges", "maximum_windows"}
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameter {unknown[0]}")
    if (
        sum(len(key.encode()) + len(value.encode()) for key, value in query.items())
        > maximum_query_bytes
    ):
        raise ValueError("full-dwell timeline query text exceeds its bound")
    try:
        maximum_windows = int(
            query.get("maximum_windows", str(MAXIMUM_FULL_DWELL_TIMELINE_WINDOWS))
        )
    except ValueError as error:
        raise ValueError("maximum_windows must be an integer") from error
    return FullDwellTimelineQueryV0_1(
        recording_id,
        tuple(
            RadioId(value) for value in (_comma_values(query, "radio_ids", 64) or ())
        ),
        tuple(
            ReceiverChainId(value)
            for value in (_comma_values(query, "receiver_chain_ids", 32) or ())
        ),
        tuple(
            StarlinkEdge(value) for value in (_comma_values(query, "edges", 2) or ())
        ),
        maximum_windows,
    )


def _doppler_aggregate_query(
    query: dict[str, str], maximum_query_bytes: int
) -> DopplerAggregateQueryV0_1:
    allowed = {
        "start_utc_ns",
        "stop_utc_ns",
        "methods",
        "models",
        "radio_ids",
        "receiver_chain_ids",
        "channels",
        "edges",
        "association_states",
    }
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameter {unknown[0]}")
    if (
        sum(len(key.encode()) + len(value.encode()) for key, value in query.items())
        > maximum_query_bytes
    ):
        raise ValueError("Doppler aggregate query text exceeds its bound")
    try:
        start = UtcNs(int(query["start_utc_ns"]))
        stop = UtcNs(int(query["stop_utc_ns"]))
    except KeyError as error:
        raise ValueError(f"missing query parameter {error.args[0]}") from error
    except (TypeError, ValueError) as error:
        raise ValueError("UTC bounds must be integers") from error

    def values(name: str, maximum: int) -> tuple[str, ...]:
        raw = _comma_values(query, name, maximum)
        if raw is None:
            return ()
        if len(set(raw)) != len(raw):
            raise ValueError(f"{name} must be unique")
        return tuple(sorted(raw))

    return DopplerAggregateQueryV0_1(
        start,
        stop,
        values("methods", 2),
        values("models", 4),
        values("radio_ids", 64),
        values("receiver_chain_ids", 32),
        values("channels", 32),
        values("edges", 3),
        values("association_states", 3),
    )


def _starlink_temporal_query(
    recording_id: RecordingId, query: dict[str, str]
) -> StarlinkTemporalPilotQueryV0_1:
    allowed = {
        "methods",
        "radio_ids",
        "receiver_chain_ids",
        "edges",
        "maximum_points",
    }
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameter {unknown[0]}")
    methods = _comma_values(query, "methods", len(REPORT_METHOD_ORDER))
    radios = _comma_values(query, "radio_ids", 16)
    receivers = _comma_values(query, "receiver_chain_ids", 16)
    edges = _comma_values(query, "edges", len(StarlinkEdge))
    return StarlinkTemporalPilotQueryV0_1(
        recording_id,
        REPORT_METHOD_ORDER
        if methods is None
        else tuple(StarlinkDetectorMethod(item) for item in methods),
        () if radios is None else tuple(RadioId(item) for item in radios),
        () if receivers is None else tuple(ReceiverChainId(item) for item in receivers),
        () if edges is None else tuple(StarlinkEdge(item) for item in edges),
        _optional_positive_int(query, "maximum_points", MAXIMUM_TEMPORAL_QUERY_POINTS),
    )


def _starlink_pilot_constellation_query(
    recording_id: RecordingId, query: dict[str, str]
) -> StarlinkPilotConstellationQueryV0_1:
    allowed = {
        "segment_ids",
        "receiver_chain_ids",
        "edges",
        "maximum_streams",
        "maximum_points_per_stream",
    }
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameter {unknown[0]}")
    query_text_bytes = sum(
        len(name.encode("utf-8")) + len(value.encode("utf-8"))
        for name, value in query.items()
    )
    if query_text_bytes > _MAX_CONSTELLATION_QUERY_TEXT_BYTES:
        raise ValueError("constellation query text exceeds its bound")

    segment_values = _comma_values(
        query, "segment_ids", _MAX_CONSTELLATION_SEGMENT_FILTERS
    )
    receiver_values = _comma_values(
        query, "receiver_chain_ids", _MAX_CONSTELLATION_RECEIVER_FILTERS
    )
    edge_values = _comma_values(query, "edges", len(StarlinkEdge))
    return StarlinkPilotConstellationQueryV0_1(
        recording_id=recording_id,
        segment_ids=(
            ()
            if segment_values is None
            else tuple(SegmentId(item) for item in segment_values)
        ),
        receiver_chain_ids=(
            ()
            if receiver_values is None
            else tuple(ReceiverChainId(item) for item in receiver_values)
        ),
        edges=(
            ()
            if edge_values is None
            else tuple(StarlinkEdge(item) for item in edge_values)
        ),
        maximum_streams=_optional_positive_int(
            query, "maximum_streams", MAX_CONSTELLATION_QUERY_STREAMS
        ),
        maximum_points_per_stream=_optional_positive_int(
            query, "maximum_points_per_stream", MAX_CONSTELLATION_POINTS
        ),
    )


def _starlink_surrogate_null_query(
    recording_id: RecordingId, query: dict[str, str]
) -> StarlinkSurrogateNullQueryV0_1:
    allowed = {
        "methods",
        "radio_ids",
        "channel_numbers",
        "edges",
        "interval_start_utc_ns",
        "interval_stop_utc_ns",
        "maximum_rows",
    }
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameter {unknown[0]}")
    query_text_bytes = sum(
        len(name.encode("utf-8")) + len(value.encode("utf-8"))
        for name, value in query.items()
    )
    if query_text_bytes > _MAX_SURROGATE_QUERY_TEXT_BYTES:
        raise ValueError("surrogate-null query text exceeds its bound")

    method_values = _comma_values(query, "methods", len(REPORT_METHOD_ORDER))
    if method_values is None:
        methods = REPORT_METHOD_ORDER
    else:
        requested_methods = tuple(
            StarlinkDetectorMethod(item) for item in method_values
        )
        if len(set(requested_methods)) != len(requested_methods):
            raise ValueError("methods must be unique")
        methods = tuple(
            method for method in REPORT_METHOD_ORDER if method in requested_methods
        )

    radio_values = _comma_values(query, "radio_ids", _MAX_SURROGATE_RADIO_FILTERS)
    radios = (
        () if radio_values is None else tuple(RadioId(item) for item in radio_values)
    )
    channel_values = _comma_values(query, "channel_numbers", 4)
    channels = (
        ()
        if channel_values is None
        else tuple(sorted(int(item) for item in channel_values))
    )
    edge_values = _comma_values(query, "edges", len(StarlinkEdge))
    edges = (
        () if edge_values is None else tuple(StarlinkEdge(item) for item in edge_values)
    )
    return StarlinkSurrogateNullQueryV0_1(
        recording_id=recording_id,
        methods=methods,
        radio_ids=radios,
        channel_numbers=channels,
        edges=edges,
        interval_start_utc_ns=_optional_utc_ns(query, "interval_start_utc_ns"),
        interval_stop_utc_ns=_optional_utc_ns(query, "interval_stop_utc_ns"),
        maximum_rows=_optional_positive_int(
            query,
            "maximum_rows",
            MAXIMUM_SURROGATE_NULL_QUERY_ROWS,
        ),
    )


def _comma_values(
    query: dict[str, str], name: str, maximum_count: int
) -> tuple[str, ...] | None:
    if name not in query:
        return None
    values = tuple(query[name].split(","))
    if not values or any(not value for value in values):
        raise ValueError(f"{name} must be a non-empty comma-separated list")
    if len(values) > maximum_count:
        raise ValueError(f"{name} exceeds its item bound")
    return values


def _optional_utc_ns(query: dict[str, str], name: str) -> UtcNs | None:
    if name not in query:
        return None
    try:
        return UtcNs(int(query[name]))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error


def _optional_positive_int(query: dict[str, str], name: str, default: int) -> int:
    if name not in query:
        return default
    try:
        value = int(query[name])
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


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
