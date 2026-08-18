from __future__ import annotations

import json
from typing import cast

from leo_flow.contracts.core import RadioId, ReceiverChainId
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV15,
    DashboardJsonApplicationV16,
    JsonRequest,
    JsonResponse,
)
from tests.contracts.test_dashboard_recording_evidence import context


class _Previous:
    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(299, (), request.path.encode())


class _Ports:
    doppler_query = None

    def recording_evidence_context(self, recording_id):
        assert str(recording_id) == "rec_evidence"
        return context()

    def recording_evidence_doppler(self, query):
        self.doppler_query = query
        return {
            "requested_recording_id": str(query.recording_id),
            "state": "missing",
            "candidate_only": True,
            "calibrated_detection_count": None,
            "series": [],
        }


def _application() -> tuple[DashboardJsonApplicationV16, _Ports]:
    ports = _Ports()
    return (
        DashboardJsonApplicationV16(
            cast(DashboardJsonApplicationV15, _Previous()), ports, ports
        ),
        ports,
    )


def test_v16_context_is_additive_bounded_and_has_authoritative_lnb() -> None:
    app, _ = _application()
    response = app.handle(
        JsonRequest("GET", "/api/v16/recordings/rec_evidence/evidence-context", {})
    )
    payload = json.loads(response.body)
    assert response.status == 200
    assert payload["receivers"][0]["lnb_id"] == "lnb_a"
    assert payload["candidate_only"] is True
    assert payload["calibrated_detection_count"] is None
    assert app.handle(JsonRequest("GET", "/older", {})).status == 299


def test_v16_passes_independent_doppler_hardware_filters() -> None:
    app, ports = _application()
    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v16/recordings/rec_evidence/evidence-doppler",
            {
                "radio_ids": "radio_a",
                "lnb_ids": "lnb_a",
                "receiver_chain_ids": "rx_a",
                "maximum_windows": "12",
            },
        )
    )
    assert response.status == 200
    assert ports.doppler_query.radio_ids == (RadioId("radio_a"),)
    assert ports.doppler_query.lnb_ids == ("lnb_a",)
    assert ports.doppler_query.receiver_chain_ids == (ReceiverChainId("rx_a"),)
    assert ports.doppler_query.maximum_windows == 12


def test_v16_rejects_unknown_unbounded_and_mutating_requests() -> None:
    app, _ = _application()
    path = "/api/v16/recordings/rec_evidence/evidence-doppler"
    assert app.handle(JsonRequest("GET", path, {"locator": "/private"})).status == 400
    assert (
        app.handle(JsonRequest("GET", path, {"maximum_windows": "4097"})).status == 400
    )
    assert app.handle(JsonRequest("POST", path, {})).status == 405
