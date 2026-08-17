from __future__ import annotations

import json

from leo_flow.contracts.core import RadioId, ReceiverChainId, RecordingId
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV13,
    JsonRequest,
    JsonResponse,
)
from leo_flow.dashboard.repository import DashboardNotFound


class _V12:
    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(299, (), request.path.encode())


class _Temporal:
    def __init__(self) -> None:
        self.query = None

    def recording_starlink_temporal_pilot(self, query):
        self.query = query
        return {
            "schema": {
                "schema_id": "org.leo-flow.dashboard.recording-starlink-temporal-pilot",
                "version": {"major": 0, "minor": 1},
            },
            "recording_id": str(query.recording_id),
            "streams": [],
            "warnings": ["candidate-evidence-not-calibrated-detection"],
        }


class _Aggregate:
    def __init__(self) -> None:
        self.query = None

    def temporal_pilot_aggregate(self, query):
        self.query = query
        return {"schema_version": 1, "recording_count": 2, "strata": []}


def test_v13_exposes_bounded_independent_radio_receiver_and_edge_filters() -> None:
    temporal = _Temporal()
    app = DashboardJsonApplicationV13(_V12(), temporal)  # type: ignore[arg-type]
    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v13/recordings/rec_temporal/starlink-temporal-pilot",
            {
                "methods": "glrt-32,anchor-8",
                "radio_ids": "radio_20",
                "receiver_chain_ids": "rx_1",
                "edges": "lower",
                "maximum_points": "17",
            },
        )
    )
    assert response.status == 200
    assert json.loads(response.body)["recording_id"] == "rec_temporal"
    assert temporal.query.recording_id == RecordingId("rec_temporal")
    assert temporal.query.methods == (
        StarlinkDetectorMethod.GLRT_32,
        StarlinkDetectorMethod.ANCHOR_8,
    )
    assert temporal.query.radio_ids == (RadioId("radio_20"),)
    assert temporal.query.receiver_chain_ids == (ReceiverChainId("rx_1"),)
    assert temporal.query.edges == (StarlinkEdge.LOWER,)
    assert temporal.query.maximum_points == 17


def test_v13_rejects_unknown_or_unbounded_queries_and_preserves_v12() -> None:
    app = DashboardJsonApplicationV13(_V12(), _Temporal())  # type: ignore[arg-type]
    assert app.handle(JsonRequest("GET", "/api/v12/old", {})).status == 299
    assert (
        app.handle(
            JsonRequest(
                "GET",
                "/api/v13/recordings/rec_x/starlink-temporal-pilot",
                {"maximum_points": "999999"},
            )
        ).status
        == 400
    )
    assert (
        app.handle(
            JsonRequest(
                "GET",
                "/api/v13/recordings/rec_x/starlink-temporal-pilot",
                {"private": "1"},
            )
        ).status
        == 400
    )


class _Missing:
    def recording_starlink_temporal_pilot(self, query):
        raise DashboardNotFound(str(query.recording_id))


def test_v13_has_explicit_missing_and_method_states() -> None:
    app = DashboardJsonApplicationV13(_V12(), _Missing())  # type: ignore[arg-type]
    path = "/api/v13/recordings/rec_missing/starlink-temporal-pilot"
    assert app.handle(JsonRequest("GET", path, {})).status == 404
    assert app.handle(JsonRequest("POST", path, {})).status == 405


def test_v13_exposes_bounded_temporal_aggregate_without_changing_recording_route() -> (
    None
):
    aggregate = _Aggregate()
    app = DashboardJsonApplicationV13(  # type: ignore[arg-type]
        _V12(), _Temporal(), aggregate
    )
    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v13/temporal-pilot-aggregate",
            {"start_utc_ns": "100", "stop_utc_ns": "200"},
        )
    )
    assert response.status == 200
    assert json.loads(response.body)["recording_count"] == 2
    assert int(aggregate.query.start_utc_ns) == 100
    assert int(aggregate.query.stop_utc_ns) == 200
