from __future__ import annotations

import json

from leo_flow.contracts.core import ArtifactRef, Digest, SchemaRef
from leo_flow.contracts.starlink_acquired_constellation_pipeline import (
    RecordingStarlinkAcquiredConstellationViewV0_3,
    StarlinkAcquiredConstellationQueryV0_3,
    StarlinkAcquiredConstellationViewMode,
)
from leo_flow.contracts.starlink_acquisition import V0_3
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV17,
    JsonRequest,
    JsonResponse,
)


class _Previous:
    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(299, (), request.path.encode())


class _Queries:
    def __init__(self) -> None:
        self.calls: list[StarlinkAcquiredConstellationQueryV0_3] = []

    def recording_starlink_acquired_constellation(
        self, query: StarlinkAcquiredConstellationQueryV0_3
    ) -> RecordingStarlinkAcquiredConstellationViewV0_3:
        self.calls.append(query)
        return RecordingStarlinkAcquiredConstellationViewV0_3(
            SchemaRef(RecordingStarlinkAcquiredConstellationViewV0_3.SCHEMA_ID, V0_3),
            query.recording_id,
            ArtifactRef(
                "slqam3rec_" + "1" * 32,
                Digest.sha256(b"bundle"),
                SchemaRef(
                    "org.leo-flow.starlink-acquired-constellation-recording-bundle",
                    V0_3,
                ),
            ),
            ArtifactRef(
                "slsuite_" + "2" * 32,
                Digest.sha256(b"suite"),
                SchemaRef(
                    "org.leo-flow.starlink-detector-suite-recording-bundle",
                    SchemaRef("x").version,
                ),
            ),
            query.mode,
            (),
            False,
            True,
            True,
        )


def test_v17_parses_window_mode_radio_lnb_and_bounds() -> None:
    queries = _Queries()
    app = DashboardJsonApplicationV17(_Previous(), queries)
    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v17/recordings/rec_qam/starlink-acquired-constellation",
            {
                "mode": "windows",
                "radio_ids": "radio_a",
                "lnb_ids": "lnb_a",
                "maximum_windows_per_stream": "8",
                "maximum_points_per_constellation": "600",
            },
        )
    )
    assert response.status == 200
    call = queries.calls[0]
    assert call.mode is StarlinkAcquiredConstellationViewMode.WINDOWS
    assert tuple(map(str, call.radio_ids)) == ("radio_a",)
    assert call.lnb_ids == ("lnb_a",)
    assert call.maximum_windows_per_stream == 8
    assert call.maximum_points_per_constellation == 600
    payload = json.loads(response.body)
    assert payload["candidate_only"] is True
    assert payload["calibration_required"] is True


def test_v17_rejects_unknown_or_unbounded_queries_and_delegates() -> None:
    app = DashboardJsonApplicationV17(_Previous(), _Queries())
    assert (
        app.handle(
            JsonRequest(
                "GET",
                "/api/v17/recordings/rec_qam/starlink-acquired-constellation",
                {"unknown": "x"},
            )
        ).status
        == 400
    )
    assert (
        app.handle(
            JsonRequest(
                "GET",
                "/api/v17/recordings/rec_qam/starlink-acquired-constellation",
                {"maximum_windows_per_stream": "33"},
            )
        ).status
        == 400
    )
    assert (
        app.handle(
            JsonRequest("GET", "/api/v15/recordings/rec_qam/starlink-full-dwell", {})
        ).status
        == 299
    )
