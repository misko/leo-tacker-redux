from __future__ import annotations

import json
from typing import cast

from leo_flow.contracts.core import UtcNs
from leo_flow.contracts.dashboard_doppler_aggregate import (
    DopplerAggregateQueryV0_1,
    DopplerAggregateViewV0_1,
)
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV13,
    DashboardJsonApplicationV14,
    JsonRequest,
    JsonResponse,
)


class _Base:
    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(299, (), request.path.encode())


class _Aggregate:
    def __init__(self) -> None:
        self.query: DopplerAggregateQueryV0_1 | None = None

    def doppler_aggregate(
        self, query: DopplerAggregateQueryV0_1
    ) -> DopplerAggregateViewV0_1:
        self.query = query
        return DopplerAggregateViewV0_1(
            1,
            query.start_utc_ns,
            query.stop_utc_ns,
            0,
            0,
            0,
            False,
            (),
            (),
            (),
            (
                "advanced-path-bins-not-converted-to-physical-frequency",
                "candidate-only-evidence-not-satellite-detection",
                "overlapping-track-observations-are-not-independent",
                "radio-and-receiver-series-are-never-pooled",
            ),
        )


def _application() -> tuple[DashboardJsonApplicationV14, _Aggregate]:
    aggregate = _Aggregate()
    return (
        DashboardJsonApplicationV14(
            cast(DashboardJsonApplicationV13, _Base()), aggregate
        ),
        aggregate,
    )


def test_v14_parses_allowlisted_filters_and_preserves_radio_receiver_separation() -> (
    None
):
    application, aggregate = _application()
    response = application.handle(
        JsonRequest(
            "GET",
            "/api/v14/doppler-aggregate",
            {
                "start_utc_ns": "1",
                "stop_utc_ns": "9",
                "methods": "advanced,basic",
                "models": "linear,slope-bank",
                "radio_ids": "radio_b,radio_a",
                "receiver_chain_ids": "rx_lnb_b,rx_lnb_a",
                "channels": "CH4",
                "edges": "lower",
                "association_states": "advanced-path-only,basic-candidate",
            },
        )
    )

    assert response.status == 200
    assert aggregate.query == DopplerAggregateQueryV0_1(
        UtcNs(1),
        UtcNs(9),
        ("advanced", "basic"),
        ("linear", "slope-bank"),
        ("radio_a", "radio_b"),
        ("rx_lnb_a", "rx_lnb_b"),
        ("CH4",),
        ("lower",),
        ("advanced-path-only", "basic-candidate"),
    )
    assert json.loads(response.body)["series"] == []


def test_v14_rejects_unknown_duplicate_invalid_and_mutating_requests() -> None:
    application, _ = _application()
    cases = (
        {"start_utc_ns": "1", "stop_utc_ns": "2", "unknown": "x"},
        {"start_utc_ns": "1", "stop_utc_ns": "2", "radio_ids": "radio_a,radio_a"},
        {"start_utc_ns": "2", "stop_utc_ns": "1"},
        {"start_utc_ns": "1", "stop_utc_ns": "2", "methods": "invented"},
    )
    for query in cases:
        response = application.handle(
            JsonRequest("GET", "/api/v14/doppler-aggregate", query)
        )
        assert response.status == 400
    assert (
        application.handle(JsonRequest("POST", "/api/v14/doppler-aggregate", {})).status
        == 405
    )


def test_v14_delegates_every_other_route_unchanged() -> None:
    application, _ = _application()
    response = application.handle(JsonRequest("GET", "/api/storage-health", {}))
    assert response.status == 299
    assert response.body == b"/api/storage-health"
