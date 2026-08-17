from __future__ import annotations

import json

from leo_flow.contracts.core import UtcNs
from leo_flow.contracts.dashboard_score_distribution import (
    PointScoreDistributionViewV0_2,
    ScoreDistributionViewV0_1,
)
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV7,
    DashboardJsonApplicationV8,
    JsonRequest,
    JsonResponse,
)


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

    def point_score_distributions(self, query):
        return PointScoreDistributionViewV0_2(
            2,
            query.start_utc_ns,
            query.stop_utc_ns,
            0.0,
            1.0,
            40,
            "recording+segment+radio+receiver-chain+edge+method",
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


def test_v8_exposes_point_identity_and_preserves_v7() -> None:
    v7 = DashboardJsonApplicationV7(_V6(), _Distributions())  # type: ignore[arg-type]
    app = DashboardJsonApplicationV8(v7, _Distributions())  # type: ignore[arg-type]
    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v8/score-distributions",
            {"start_utc_ns": "1", "stop_utc_ns": "2"},
        )
    )
    assert response.status == 200
    assert json.loads(response.body)["point_identity"] == (
        "recording+segment+radio+receiver-chain+edge+method"
    )
    assert (
        app.handle(
            JsonRequest(
                "GET",
                "/api/v7/score-distributions",
                {"start_utc_ns": "1", "stop_utc_ns": "2"},
            )
        ).status
        == 200
    )
