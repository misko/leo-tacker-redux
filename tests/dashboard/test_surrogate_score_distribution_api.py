from __future__ import annotations

import json

from leo_flow.contracts.core import UtcNs
from leo_flow.contracts.dashboard_surrogate_distribution import (
    SurrogateScoreDistributionViewV0_1,
)
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV12,
    JsonRequest,
    JsonResponse,
)


class _V11:
    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(418, (), request.path.encode())


class _Distributions:
    def surrogate_score_distributions(self, query):
        return SurrogateScoreDistributionViewV0_1(
            1,
            query.start_utc_ns,
            query.stop_utc_ns,
            40,
            "recording+segment+radio+receiver-chain+edge+method+pattern",
            0,
            False,
            (),
            (
                "finite-surrogate-ensemble-not-calibrated-null-distribution",
                "candidate-evidence-not-detection",
            ),
        )


def test_v12_exposes_surrogate_distributions_and_preserves_v11() -> None:
    app = DashboardJsonApplicationV12(_V11(), _Distributions())  # type: ignore[arg-type]
    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v12/surrogate-score-distributions",
            {"start_utc_ns": "1", "stop_utc_ns": "2"},
        )
    )
    assert response.status == 200
    assert json.loads(response.body)["point_identity"].endswith("+pattern")
    assert app.handle(JsonRequest("GET", "/api/v11/legacy", {})).status == 418


def test_v12_rejects_invalid_bounds_and_writes() -> None:
    app = DashboardJsonApplicationV12(_V11(), _Distributions())  # type: ignore[arg-type]
    assert (
        app.handle(
            JsonRequest("GET", "/api/v12/surrogate-score-distributions", {})
        ).status
        == 400
    )
    assert (
        app.handle(
            JsonRequest(
                "POST",
                "/api/v12/surrogate-score-distributions",
                {
                    "start_utc_ns": str(UtcNs(1)),
                    "stop_utc_ns": str(UtcNs(2)),
                },
            )
        ).status
        == 405
    )
