from __future__ import annotations

import json
from typing import cast

from leo_flow.contracts.core import RadioId, ReceiverChainId
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV27,
    DashboardJsonApplicationV28,
    JsonRequest,
    JsonResponse,
)


class _Previous:
    def handle(self, request: JsonRequest) -> JsonResponse:
        del request
        return JsonResponse(418, (), b"")


class _Refinements:
    query = None

    def recording_starlink_pilot_refinement(self, query):  # type: ignore[no-untyped-def]
        self.query = query
        return {
            "recording_id": str(query.recording_id),
            "candidate_only": True,
            "calibration_required": True,
            "streams": [],
        }


def test_v28_routes_bounded_unpooled_pilot_refinement_query() -> None:
    port = _Refinements()
    app = DashboardJsonApplicationV28(
        cast(DashboardJsonApplicationV27, _Previous()), port
    )

    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v28/recordings/rec_refinement/starlink-pilot-refinement",
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
    assert json.loads(response.body)["recording_id"] == "rec_refinement"
    assert port.query.methods == (
        StarlinkDetectorMethod.GLRT_32,
        StarlinkDetectorMethod.FULL_FRAME_ACQUIRE,
    )
    assert port.query.radio_ids == (RadioId("radio_a"),)
    assert port.query.lnb_ids == ("lnb-a",)
    assert port.query.receiver_chain_ids == (ReceiverChainId("rx_a"),)
    assert port.query.edges == (StarlinkEdge.LOWER,)
    assert port.query.maximum_points == 8192


def test_v28_rejects_unknown_unbounded_and_mutating_requests() -> None:
    app = DashboardJsonApplicationV28(
        cast(DashboardJsonApplicationV27, _Previous()), _Refinements()
    )
    for query in ({"maximum_points": "16385"}, {"locator": "/private"}):
        assert (
            app.handle(
                JsonRequest(
                    "GET",
                    "/api/v28/recordings/rec_refinement/starlink-pilot-refinement",
                    query,
                )
            ).status
            == 400
        )
    assert (
        app.handle(
            JsonRequest(
                "POST",
                "/api/v28/recordings/rec_refinement/starlink-pilot-refinement",
                {},
            )
        ).status
        == 405
    )
