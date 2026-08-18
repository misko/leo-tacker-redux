from __future__ import annotations

import json
from typing import cast

from leo_flow.contracts.core import RadioId, ReceiverChainId
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV14,
    DashboardJsonApplicationV15,
    JsonRequest,
)


class _Base:
    def handle(self, request: JsonRequest):
        raise AssertionError(f"unexpected delegation: {request.path}")


class _FullDwell:
    query = None

    def recording_starlink_full_dwell(self, query):
        self.query = query
        return {
            "recording_id": str(query.recording_id),
            "streams": [],
            "queue_state": "complete",
            "backlog_depth": 0,
            "warnings": ["exact-detector-windows-are-selected-not-full-coverage"],
        }


def test_v15_is_additive_and_passes_independent_bounded_filters() -> None:
    port = _FullDwell()
    app = DashboardJsonApplicationV15(cast(DashboardJsonApplicationV14, _Base()), port)
    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v15/recordings/rec_fd/starlink-full-dwell",
            {
                "methods": "glrt-32",
                "radio_ids": "radio_a",
                "receiver_chain_ids": "rx_1",
                "edges": "lower",
                "maximum_points": "32",
            },
        )
    )
    assert response.status == 200
    assert json.loads(response.body)["queue_state"] == "complete"
    assert port.query.methods == (StarlinkDetectorMethod.GLRT_32,)
    assert port.query.radio_ids == (RadioId("radio_a"),)
    assert port.query.receiver_chain_ids == (ReceiverChainId("rx_1"),)
    assert port.query.edges == (StarlinkEdge.LOWER,)
    assert port.query.maximum_points == 32


def test_v15_rejects_unbounded_or_unknown_queries() -> None:
    app = DashboardJsonApplicationV15(
        cast(DashboardJsonApplicationV14, _Base()), _FullDwell()
    )
    for query in ({"maximum_points": "4097"}, {"locator": "/cas/private"}):
        response = app.handle(
            JsonRequest("GET", "/api/v15/recordings/rec_fd/starlink-full-dwell", query)
        )
        assert response.status == 400
