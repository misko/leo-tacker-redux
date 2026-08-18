from __future__ import annotations

import json
from typing import Any, cast

from leo_flow.contracts.core import RadioId, ReceiverChainId, SegmentId
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_acquired_constellation_pipeline import (
    StarlinkAcquiredConstellationQueryV0_3,
    StarlinkAcquiredConstellationViewMode,
)
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV25,
    JsonRequest,
    JsonResponse,
)


class _Previous:
    def handle(self, request: JsonRequest) -> JsonResponse:
        del request
        return JsonResponse(418, (), b"")


class _AdaptiveQam:
    query: StarlinkAcquiredConstellationQueryV0_3 | None = None

    def recording_starlink_adaptive_qam(
        self, query: StarlinkAcquiredConstellationQueryV0_3
    ) -> dict[str, object]:
        self.query = query
        return {
            "recording_id": str(query.recording_id),
            "mode": query.mode.value,
            "candidate_only": True,
            "calibration_required": True,
            "streams": [],
        }


def test_v25_routes_bounded_adaptive_qam_query() -> None:
    port = _AdaptiveQam()
    app = DashboardJsonApplicationV25(_Previous(), cast(Any, port))

    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v25/recordings/rec_adaptive/starlink-adaptive-qam",
            {
                "mode": "windows",
                "radio_ids": "radio_a",
                "lnb_ids": "lnb-a",
                "segment_ids": "seg_a",
                "receiver_chain_ids": "rx_a",
                "edges": "lower",
                "maximum_streams": "4",
                "maximum_windows_per_stream": "12",
                "maximum_points_per_constellation": "128",
            },
        )
    )

    assert response.status == 200
    assert json.loads(response.body)["recording_id"] == "rec_adaptive"
    query = port.query
    assert query is not None
    assert query.mode is StarlinkAcquiredConstellationViewMode.WINDOWS
    assert query.radio_ids == (RadioId("radio_a"),)
    assert query.lnb_ids == ("lnb-a",)
    assert query.segment_ids == (SegmentId("seg_a"),)
    assert query.receiver_chain_ids == (ReceiverChainId("rx_a"),)
    assert query.edges == (StarlinkEdge.LOWER,)
    assert query.maximum_streams == 4
    assert query.maximum_windows_per_stream == 12
    assert query.maximum_points_per_constellation == 128


def test_v25_rejects_oversized_unknown_and_mutating_requests() -> None:
    app = DashboardJsonApplicationV25(_Previous(), cast(Any, _AdaptiveQam()))
    path = "/api/v25/recordings/rec_adaptive/starlink-adaptive-qam"
    for query in (
        {"maximum_windows_per_stream": "33"},
        {"maximum_points_per_constellation": "2401"},
        {"locator": "/private"},
    ):
        assert app.handle(JsonRequest("GET", path, query)).status == 400
    assert app.handle(JsonRequest("POST", path, {})).status == 405
