from __future__ import annotations

import json

from leo_flow.contracts.dashboard_observation import ObservationAggregateViewV0_1
from leo_flow.dashboard.api import DashboardJsonApplicationV6, JsonRequest, JsonResponse


class _V5:
    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(418, (), request.path.encode())


class _Aggregate:
    def observation_aggregate(self, query):
        return ObservationAggregateViewV0_1(
            1,
            query.start_utc_ns,
            query.stop_utc_ns,
            0,
            0,
            0,
            0,
            "required",
            "whole-search-calibration-required",
            (),
            (),
            (),
            False,
        )


def test_v6_exposes_truthful_aggregate_and_preserves_v5() -> None:
    app = DashboardJsonApplicationV6(_V5(), _Aggregate())  # type: ignore[arg-type]
    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v6/observation-aggregate",
            {"start_utc_ns": "1", "stop_utc_ns": "2"},
        )
    )
    assert response.status == 200
    assert json.loads(response.body)["calibration_status"] == "required"
    assert app.handle(JsonRequest("GET", "/api/storage-health", {})).status == 418


def test_v6_rejects_missing_bounds_and_writes() -> None:
    app = DashboardJsonApplicationV6(_V5(), _Aggregate())  # type: ignore[arg-type]
    assert (
        app.handle(JsonRequest("GET", "/api/v6/observation-aggregate", {})).status
        == 400
    )
    assert (
        app.handle(JsonRequest("POST", "/api/v6/observation-aggregate", {})).status
        == 405
    )
