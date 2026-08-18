from __future__ import annotations

import json

from leo_flow.dashboard.api import (
    DashboardJsonApplicationCaptures,
    JsonRequest,
    JsonResponse,
)


class _Previous:
    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(299, (), request.path.encode())


class _Captures:
    query = None
    cursor = None

    def master_capture_snapshot(self, query, cursor=None):
        self.query, self.cursor = query, cursor
        return {
            "schema_version": 1,
            "start_utc_ns": query.start_utc_ns,
            "stop_utc_ns": query.stop_utc_ns,
            "items": [],
            "next_cursor": None,
            "observation_aggregate": {"state": "complete", "value": {}},
            "retro_qam_canary": {"state": "unavailable", "value": None},
            "warnings": [],
        }


def test_captures_route_is_one_bounded_snapshot_query_and_preserves_previous() -> None:
    captures = _Captures()
    app = DashboardJsonApplicationCaptures(_Previous(), captures)
    response = app.handle(
        JsonRequest(
            "GET",
            "/api/captures",
            {
                "start_utc_ns": "10",
                "stop_utc_ns": "20",
                "maximum_recordings": "12",
                "cursor": "opaque",
            },
        )
    )

    assert response.status == 200
    assert captures.query.maximum_recordings == 12
    assert captures.cursor == "opaque"
    assert json.loads(response.body)["items"] == []
    assert json.loads(response.body)["retro_qam_canary"]["state"] == "unavailable"
    assert app.handle(JsonRequest("GET", "/api/v30/older", {})).status == 299


def test_captures_route_rejects_mutation_unknown_and_unbounded_queries() -> None:
    app = DashboardJsonApplicationCaptures(_Previous(), _Captures())
    assert app.handle(JsonRequest("POST", "/api/captures", {})).status == 405
    for query in (
        {"start_utc_ns": "1", "stop_utc_ns": "2", "locator": "/private"},
        {
            "start_utc_ns": "1",
            "stop_utc_ns": "2",
            "maximum_recordings": "101",
        },
    ):
        assert app.handle(JsonRequest("GET", "/api/captures", query)).status == 400
