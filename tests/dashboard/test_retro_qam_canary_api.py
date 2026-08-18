from __future__ import annotations

import json
from typing import cast

from leo_flow.dashboard.api import (
    DashboardJsonApplicationV20,
    DashboardJsonApplicationV21,
    JsonRequest,
)


class _Base:
    def handle(self, request: JsonRequest):
        raise AssertionError(f"unexpected delegation: {request.path}")


class _Canary:
    def latest_retro_qam_canary(self):
        return {
            "metrics_match_oracle": True,
            "combined_qam_goodness": 0.875,
            "candidate_only": True,
            "calibrated_detection": None,
        }


def test_v21_returns_latest_historical_acceptance_canary() -> None:
    app = DashboardJsonApplicationV21(
        cast(DashboardJsonApplicationV20, _Base()), _Canary()
    )
    response = app.handle(JsonRequest("GET", "/api/v21/canaries/retro-qam/latest", {}))
    assert response.status == 200
    assert json.loads(response.body)["combined_qam_goodness"] == 0.875


def test_v21_rejects_query_parameters_and_writes() -> None:
    app = DashboardJsonApplicationV21(
        cast(DashboardJsonApplicationV20, _Base()), _Canary()
    )
    assert (
        app.handle(
            JsonRequest(
                "GET", "/api/v21/canaries/retro-qam/latest", {"path": "/private"}
            )
        ).status
        == 400
    )
    assert (
        app.handle(JsonRequest("POST", "/api/v21/canaries/retro-qam/latest", {})).status
        == 405
    )
