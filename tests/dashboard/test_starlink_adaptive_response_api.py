from __future__ import annotations

import json
from typing import cast

from leo_flow.contracts.core import RadioId, ReceiverChainId
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV23,
    DashboardJsonApplicationV24,
    JsonRequest,
    JsonResponse,
)


class _Previous:
    def handle(self, request: JsonRequest) -> JsonResponse:
        del request
        return JsonResponse(418, (), b"")


class _Responses:
    query = None

    def recording_starlink_adaptive_response(self, query):
        self.query = query
        return {
            "recording_id": str(query.recording_id),
            "candidate_only": True,
            "calibration_required": True,
            "streams": [],
        }


def test_v24_routes_bounded_unpooled_adaptive_response_query() -> None:
    port = _Responses()
    app = DashboardJsonApplicationV24(
        cast(DashboardJsonApplicationV23, _Previous()), port
    )

    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v24/recordings/rec_adaptive/starlink-adaptive-response",
            {
                "methods": "glrt-32,full-frame-acquire",
                "radio_ids": "radio_a",
                "lnb_ids": "lnb-a",
                "receiver_chain_ids": "rx_a",
                "edges": "lower",
                "maximum_points": "8192",
            },
        )
    )

    assert response.status == 200
    assert json.loads(response.body)["recording_id"] == "rec_adaptive"
    assert port.query.methods == (
        StarlinkDetectorMethod.GLRT_32,
        StarlinkDetectorMethod.FULL_FRAME_ACQUIRE,
    )
    assert port.query.radio_ids == (RadioId("radio_a"),)
    assert port.query.lnb_ids == ("lnb-a",)
    assert port.query.receiver_chain_ids == (ReceiverChainId("rx_a"),)
    assert port.query.edges == (StarlinkEdge.LOWER,)
    assert port.query.maximum_points == 8192


def test_v24_rejects_unknown_unbounded_and_mutating_requests() -> None:
    app = DashboardJsonApplicationV24(
        cast(DashboardJsonApplicationV23, _Previous()), _Responses()
    )
    for query in ({"maximum_points": "16385"}, {"locator": "/private"}):
        assert (
            app.handle(
                JsonRequest(
                    "GET",
                    "/api/v24/recordings/rec_adaptive/starlink-adaptive-response",
                    query,
                )
            ).status
            == 400
        )
    assert (
        app.handle(
            JsonRequest(
                "POST",
                "/api/v24/recordings/rec_adaptive/starlink-adaptive-response",
                {},
            )
        ).status
        == 405
    )
