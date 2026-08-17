from __future__ import annotations

import json

from leo_flow.contracts.core import UtcNs
from leo_flow.contracts.dashboard_score_distribution import ScoreDistributionViewV0_1
from leo_flow.dashboard.api import DashboardJsonApplicationV7, JsonRequest, JsonResponse


class _V6:
    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(418, (), request.path.encode())


class _Distributions:
    def score_distributions(self, query):
        return ScoreDistributionViewV0_1(
            1,
            query.start_utc_ns,
            query.stop_utc_ns,
            0.0,
            1.0,
            40,
            "candidate-method-score-density",
            (),
        )


def test_v7_exposes_score_distributions_and_preserves_v6() -> None:
    app = DashboardJsonApplicationV7(_V6(), _Distributions())  # type: ignore[arg-type]
    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v7/score-distributions",
            {"start_utc_ns": "1", "stop_utc_ns": "2"},
        )
    )
    assert response.status == 200
    assert json.loads(response.body)["score_domain_upper"] == 1.0
    assert (
        app.handle(JsonRequest("GET", "/api/v6/observation-aggregate", {})).status
        == 418
    )


def test_v7_rejects_invalid_bounds_and_writes() -> None:
    app = DashboardJsonApplicationV7(_V6(), _Distributions())  # type: ignore[arg-type]
    assert (
        app.handle(JsonRequest("GET", "/api/v7/score-distributions", {})).status == 400
    )
    assert (
        app.handle(
            JsonRequest(
                "POST",
                "/api/v7/score-distributions",
                {"start_utc_ns": str(UtcNs(1)), "stop_utc_ns": str(UtcNs(2))},
            )
        ).status
        == 405
    )
