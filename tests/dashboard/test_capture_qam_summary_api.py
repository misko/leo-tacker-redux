from __future__ import annotations

import json

from leo_flow.dashboard.api import (
    DashboardJsonApplicationV22,
    JsonRequest,
    JsonResponse,
)


class _Previous:
    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(299, (), request.path.encode())


class _Summaries:
    query = None

    def capture_qam_summaries(self, query):
        self.query = query
        return {
            "candidate_only": True,
            "calibration_required": True,
            "calibrated_detection_count": None,
            "recordings": [],
            "warnings": ["radio-lnb-receiver-series-are-never-pooled"],
        }


def test_v22_is_one_bounded_bulk_query_and_preserves_previous_routes() -> None:
    summaries = _Summaries()
    app = DashboardJsonApplicationV22(_Previous(), summaries)
    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v22/capture-qam-summaries",
            {"start_utc_ns": "10", "stop_utc_ns": "20", "maximum_recordings": "12"},
        )
    )
    assert response.status == 200
    assert summaries.query.maximum_recordings == 12
    assert json.loads(response.body)["calibration_required"] is True
    assert app.handle(JsonRequest("GET", "/older", {})).status == 299


def test_v22_rejects_mutation_unknown_and_unbounded_queries() -> None:
    app = DashboardJsonApplicationV22(_Previous(), _Summaries())
    path = "/api/v22/capture-qam-summaries"
    assert app.handle(JsonRequest("POST", path, {})).status == 405
    assert (
        app.handle(
            JsonRequest(
                "GET",
                path,
                {"start_utc_ns": "1", "stop_utc_ns": "2", "locator": "/private"},
            )
        ).status
        == 400
    )
    assert (
        app.handle(
            JsonRequest(
                "GET",
                path,
                {"start_utc_ns": "1", "stop_utc_ns": "2", "maximum_recordings": "101"},
            )
        ).status
        == 400
    )
