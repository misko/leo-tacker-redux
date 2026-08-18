from __future__ import annotations

import json
from typing import Any, cast

from leo_flow.contracts.core import RadioId, ReceiverChainId
from leo_flow.contracts.dashboard_pilot_doppler import (
    PilotDopplerAssociationQueryV0_1,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV26,
    JsonRequest,
    JsonResponse,
)


class _Previous:
    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(299, (), request.path.encode())


class _Associations:
    query: PilotDopplerAssociationQueryV0_1 | None = None

    def recording_pilot_doppler_association(
        self, query: PilotDopplerAssociationQueryV0_1
    ) -> dict[str, object]:
        self.query = query
        return {
            "recording_id": str(query.recording_id),
            "state": "complete",
            "frequency_gate_hz": 50_000,
            "series": [],
            "candidate_only": True,
            "calibrated_detection_count": None,
        }


def test_v26_routes_bounded_hardware_and_edge_filters() -> None:
    port = _Associations()
    app = DashboardJsonApplicationV26(_Previous(), cast(Any, port))

    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v26/recordings/rec_pilot/pilot-doppler-association",
            {
                "radio_ids": "radio_a",
                "lnb_ids": "lnb-current",
                "receiver_chain_ids": "rx_a",
                "edges": "lower",
                "maximum_windows_per_stream": "12",
            },
        )
    )

    assert response.status == 200
    assert json.loads(response.body)["candidate_only"] is True
    assert port.query == PilotDopplerAssociationQueryV0_1(
        port.query.recording_id,
        (RadioId("radio_a"),),
        ("lnb-current",),
        (ReceiverChainId("rx_a"),),
        (StarlinkEdge.LOWER,),
        12,
    )
    assert app.handle(JsonRequest("GET", "/older", {})).status == 299


def test_v26_rejects_unknown_unbounded_and_mutating_requests() -> None:
    app = DashboardJsonApplicationV26(_Previous(), cast(Any, _Associations()))
    path = "/api/v26/recordings/rec_pilot/pilot-doppler-association"

    assert app.handle(JsonRequest("POST", path, {})).status == 405
    assert app.handle(JsonRequest("GET", path, {"locator": "/private"})).status == 400
    assert (
        app.handle(
            JsonRequest("GET", path, {"maximum_windows_per_stream": "33"})
        ).status
        == 400
    )
