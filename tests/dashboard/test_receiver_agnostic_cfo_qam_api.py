from __future__ import annotations

import json
from typing import cast

from leo_flow.contracts.core import RadioId, ReceiverChainId
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV29,
    DashboardJsonApplicationV30,
    JsonRequest,
    JsonResponse,
)


class _Previous:
    def handle(self, request: JsonRequest) -> JsonResponse:
        del request
        return JsonResponse(418, (), b"")


class _Port:
    query = None

    def recording_receiver_agnostic_cfo_qam(self, query):  # type: ignore[no-untyped-def]
        self.query = query
        return {
            "recording_id": str(query.recording_id),
            "candidates_only": True,
            "calibrated_detection_count": None,
            "windows": [],
        }


def test_v30_routes_only_bounded_receiver_agnostic_filters() -> None:
    port = _Port()
    app = DashboardJsonApplicationV30(
        cast(DashboardJsonApplicationV29, _Previous()), port
    )
    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v30/recordings/rec_a/receiver-agnostic-cfo-qam",
            {
                "radio_ids": "radio_a,radio_b",
                "receiver_chain_ids": "rx_a,rx_b",
                "maximum_windows": "4",
            },
        )
    )
    assert response.status == 200 and json.loads(response.body)["candidates_only"]
    assert port.query.radio_ids == (RadioId("radio_a"), RadioId("radio_b"))
    assert port.query.receiver_chain_ids == (
        ReceiverChainId("rx_a"),
        ReceiverChainId("rx_b"),
    )
    assert port.query.maximum_windows == 4


def test_v30_preserves_v29_and_rejects_labels_centers_and_unbounded_queries() -> None:
    app = DashboardJsonApplicationV30(
        cast(DashboardJsonApplicationV29, _Previous()), _Port()
    )
    assert app.handle(JsonRequest("GET", "/api/v29/unchanged", {})).status == 418
    for query in (
        {"lnb_ids": "lnb-a"},
        {"frequency_center_hz": "1"},
        {"maximum_windows": "7"},
        {"radio_ids": "b,a"},
    ):
        assert (
            app.handle(
                JsonRequest(
                    "GET", "/api/v30/recordings/rec_a/receiver-agnostic-cfo-qam", query
                )
            ).status
            == 400
        )

    class _Pending:
        def recording_receiver_agnostic_cfo_qam(self, query):  # type: ignore[no-untyped-def]
            del query
            raise LookupError("offline product not published")

    pending = DashboardJsonApplicationV30(
        cast(DashboardJsonApplicationV29, _Previous()), _Pending()
    ).handle(
        JsonRequest(
            "GET",
            "/api/v30/recordings/rec_a/receiver-agnostic-cfo-qam",
            {},
        )
    )
    assert pending.status == 404
