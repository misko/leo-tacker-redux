from __future__ import annotations

import json
from typing import cast

from leo_flow.contracts.core import RecordingId
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV22,
    DashboardJsonApplicationV23,
    JsonRequest,
    JsonResponse,
)


class _Previous:
    def handle(self, request: JsonRequest) -> JsonResponse:
        del request
        return JsonResponse(418, (), b"")


class _Approaches:
    def __init__(self) -> None:
        self.recording_ids: list[RecordingId] = []

    def recording_analysis_approach(
        self, recording_id: RecordingId
    ) -> dict[str, object]:
        self.recording_ids.append(recording_id)
        return {
            "recording_id": recording_id,
            "candidate_only": True,
            "calibration_required": True,
            "qam_streams": [],
        }


def test_v23_routes_exact_recording_approach_without_query_parameters() -> None:
    port = _Approaches()
    app = DashboardJsonApplicationV23(
        cast(DashboardJsonApplicationV22, _Previous()), port
    )

    response = app.handle(
        JsonRequest("GET", "/api/v23/recordings/rec_current/analysis-approaches", {})
    )

    assert response.status == 200
    assert json.loads(response.body)["recording_id"] == "rec_current"
    assert port.recording_ids == [RecordingId("rec_current")]
    assert (
        app.handle(
            JsonRequest(
                "GET",
                "/api/v23/recordings/rec_current/analysis-approaches",
                {"legacy_lnb_offset_hz": "602869.4"},
            )
        ).status
        == 400
    )
    assert (
        app.handle(
            JsonRequest(
                "POST", "/api/v23/recordings/rec_current/analysis-approaches", {}
            )
        ).status
        == 405
    )
    assert (
        app.handle(
            JsonRequest("GET", "/api/v23/recordings/rec_current/other", {})
        ).status
        == 404
    )
