from __future__ import annotations

import json
from typing import cast

from leo_flow.contracts.core import RadioId, ReceiverChainId
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV28,
    DashboardJsonApplicationV29,
    JsonRequest,
    JsonResponse,
)


class _Previous:
    def handle(self, request: JsonRequest) -> JsonResponse:
        del request
        return JsonResponse(418, (), b"")


class _Symbolwise:
    query = None

    def recording_symbolwise_replay_dashboard(self, query):  # type: ignore[no-untyped-def]
        self.query = query
        return {
            "recording_id": str(query.recording_id),
            "stream_count": 0,
            "window_count_per_stream": 600,
            "point_count": 0,
            "candidate_only": True,
            "calibrated_detection_count": None,
            "streams": [],
        }


def test_v29_routes_only_canonical_hardware_filters_to_the_narrow_port() -> None:
    port = _Symbolwise()
    app = DashboardJsonApplicationV29(
        cast(DashboardJsonApplicationV28, _Previous()), port
    )
    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v29/recordings/rec_symbolwise/symbolwise-replay",
            {
                "radio_ids": "radio_a,radio_b",
                "lnb_ids": "lnb-a,lnb-b",
                "receiver_chain_ids": "rx_a,rx_b",
            },
        )
    )
    assert response.status == 200
    assert json.loads(response.body)["candidate_only"] is True
    assert port.query.radio_ids == (RadioId("radio_a"), RadioId("radio_b"))
    assert port.query.lnb_ids == ("lnb-a", "lnb-b")
    assert port.query.receiver_chain_ids == (
        ReceiverChainId("rx_a"),
        ReceiverChainId("rx_b"),
    )


def test_v29_preserves_v28_and_has_bounded_redacted_errors() -> None:
    app = DashboardJsonApplicationV29(
        cast(DashboardJsonApplicationV28, _Previous()), _Symbolwise()
    )
    assert app.handle(JsonRequest("GET", "/api/v28/unchanged", {})).status == 418
    assert app.handle(
        JsonRequest(
            "GET",
            "/api/v29/recordings/rec_symbolwise/symbolwise-replay",
            {"locator": "/private/catalog/path"},
        )
    ).status == 400
    assert app.handle(
        JsonRequest(
            "GET",
            "/api/v29/recordings/rec_symbolwise/symbolwise-replay",
            {"radio_ids": "radio_b,radio_a"},
        )
    ).status == 400
    assert app.handle(
        JsonRequest(
            "POST",
            "/api/v29/recordings/rec_symbolwise/symbolwise-replay",
            {},
        )
    ).status == 405

    class _Broken:
        def recording_symbolwise_replay_dashboard(self, query):  # type: ignore[no-untyped-def]
            del query
            raise RuntimeError("secret CAS locator")

    broken = DashboardJsonApplicationV29(
        cast(DashboardJsonApplicationV28, _Previous()), _Broken()
    ).handle(
        JsonRequest(
            "GET",
            "/api/v29/recordings/rec_symbolwise/symbolwise-replay",
            {},
        )
    )
    assert broken.status == 500
    assert b"secret" not in broken.body and b"locator" not in broken.body
