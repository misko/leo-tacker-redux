from __future__ import annotations

import json
from typing import cast

from leo_flow.contracts.core import RadioId, ReceiverChainId
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV19,
    DashboardJsonApplicationV20,
    JsonRequest,
)


class _Base:
    def handle(self, request: JsonRequest):
        raise AssertionError(f"unexpected delegation: {request.path}")


class _Timelines:
    query = None

    def recording_full_dwell_timeline(self, query):
        self.query = query
        return {
            "recording_id": str(query.recording_id),
            "original_window_count": 15_000,
            "returned_window_count": 15_000,
            "candidate_only": True,
            "calibrated_detection_count": None,
            "streams": [],
        }


def test_v20_passes_bounded_unpooled_timeline_filters() -> None:
    port = _Timelines()
    app = DashboardJsonApplicationV20(cast(DashboardJsonApplicationV19, _Base()), port)
    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v20/recordings/rec_fd/full-dwell-timeline",
            {
                "radio_ids": "radio_a",
                "receiver_chain_ids": "rx_1",
                "edges": "lower",
                "maximum_windows": "16000",
            },
        )
    )
    assert response.status == 200
    assert json.loads(response.body)["returned_window_count"] == 15_000
    assert port.query.radio_ids == (RadioId("radio_a"),)
    assert port.query.receiver_chain_ids == (ReceiverChainId("rx_1"),)
    assert port.query.edges == (StarlinkEdge.LOWER,)
    assert port.query.maximum_windows == 16_000


def test_v20_rejects_unknown_and_unbounded_queries() -> None:
    app = DashboardJsonApplicationV20(
        cast(DashboardJsonApplicationV19, _Base()), _Timelines()
    )
    for query in ({"maximum_windows": "16385"}, {"locator": "/private"}):
        response = app.handle(
            JsonRequest(
                "GET",
                "/api/v20/recordings/rec_fd/full-dwell-timeline",
                query,
            )
        )
        assert response.status == 400
