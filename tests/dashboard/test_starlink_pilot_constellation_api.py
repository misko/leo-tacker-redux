from __future__ import annotations

import json

import pytest

from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    Provenance,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod
from leo_flow.contracts.starlink_pilot_constellation import (
    MAX_CONSTELLATION_POINTS,
    StarlinkPilotConstellationEvidenceV0_1,
    StarlinkPilotConstellationPointV0_1,
    StarlinkPilotSubcarrierSummaryV0_1,
)
from leo_flow.contracts.starlink_pilot_constellation_pipeline import (
    MAX_CONSTELLATION_QUERY_STREAMS,
    RecordingStarlinkPilotConstellationViewV0_1,
    StarlinkPilotConstellationQueryV0_1,
    constellation_presentation_stream,
)
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV11,
    JsonRequest,
    JsonResponse,
)
from leo_flow.dashboard.repository import DashboardNotFound

RECORDING_ID = RecordingId("rec_constellation")
SEGMENT_ID = SegmentId("seg_constellation")
RECEIVER_CHAIN_ID = ReceiverChainId("rx_constellation")


def _digest(value: str) -> Digest:
    return Digest.sha256(value.encode())


def _artifact(value: str, schema: str = "org.leo-flow.test-artifact") -> ArtifactRef:
    return ArtifactRef(value, _digest(value), SchemaRef(schema))


def _provenance() -> Provenance:
    return Provenance(
        "pilot-constellation-analyzer",
        "0.1.0",
        "abc123",
        _digest("environment"),
        _digest("config"),
        (_digest("input"),),
        (_digest("dependency"),),
        UtcNs(1_000_000_000),
        UtcNs(2_000_000_000),
        "test-host",
    )


def evidence(
    *,
    segment_id: SegmentId = SEGMENT_ID,
    receiver_chain_id: ReceiverChainId = RECEIVER_CHAIN_ID,
    edge: StarlinkEdge = StarlinkEdge.LOWER,
) -> StarlinkPilotConstellationEvidenceV0_1:
    indexes = (
        tuple(range(528, 536)) if edge is StarlinkEdge.LOWER else tuple(range(488, 496))
    )
    points = []
    for symbol_index in range(2, 302):
        for offset, subcarrier in enumerate(indexes):
            state = (symbol_index + offset) % 4
            correct = (symbol_index + offset) % 17 != 0
            points.append(
                StarlinkPilotConstellationPointV0_1(
                    symbol_index,
                    subcarrier,
                    state,
                    state if correct else (state + 1) % 4,
                    (0.707 if state in (0, 3) else -0.707)
                    + ((symbol_index % 7) - 3) * 0.006,
                    (0.707 if state in (0, 1) else -0.707) + ((offset % 5) - 2) * 0.007,
                    correct,
                    0.9,
                    0.85,
                    0.4,
                )
            )
    subcarriers = tuple(
        StarlinkPilotSubcarrierSummaryV0_1(
            subcarrier,
            (index - 3.5) * 2_343.75,
            0.95 - index * 0.01,
            0.12 + index * 0.01,
            1.0 + index * 0.02,
            -10.0 + index,
        )
        for index, subcarrier in enumerate(indexes)
    )
    return StarlinkPilotConstellationEvidenceV0_1(
        SchemaRef(StarlinkPilotConstellationEvidenceV0_1.SCHEMA_ID),
        "slqam_test",
        RECORDING_ID,
        _digest("recording"),
        segment_id,
        receiver_chain_id,
        edge,
        2_500_000.0,
        20_000,
        "slsuite_test",
        _digest("suite"),
        _digest("suite-identity"),
        StarlinkDetectorMethod.FULL_FRAME_ACQUIRE,
        _digest("search"),
        _artifact("acquire-algorithm"),
        _artifact("acquire-config"),
        _artifact("qin-template"),
        123,
        1_250.0,
        -75.0,
        4.5,
        5,
        4.2,
        6.23,
        2_400,
        0.91,
        0.25,
        0.18,
        1.02,
        0.92,
        0.88,
        0.37,
        0.03,
        14.2,
        subcarriers,
        tuple(points),
        "quality-weighted-stack-all-300x8-cross-fitted",
        _provenance(),
        True,
        True,
        False,
        (
            "candidate-evidence-not-calibrated-detection",
            "published-edge-pilot-not-user-payload",
            "conditioned-on-full-frame-acquire-winner",
        ),
    )


def view(
    query: StarlinkPilotConstellationQueryV0_1,
) -> RecordingStarlinkPilotConstellationViewV0_1:
    segment = (
        query.segment_ids[0] if query.segment_ids else SegmentId("seg_constellation")
    )
    receiver = (
        query.receiver_chain_ids[0]
        if query.receiver_chain_ids
        else ReceiverChainId("rx_constellation")
    )
    edge = query.edges[0] if query.edges else StarlinkEdge.LOWER
    return RecordingStarlinkPilotConstellationViewV0_1(
        SchemaRef(RecordingStarlinkPilotConstellationViewV0_1.SCHEMA_ID),
        query.recording_id,
        _artifact(
            "slqamrec_test",
            "org.leo-flow.starlink-pilot-constellation-recording-bundle",
        ),
        _artifact(
            "slsuite_test",
            "org.leo-flow.starlink-detector-suite-recording-bundle",
        ),
        (
            constellation_presentation_stream(
                evidence(segment_id=segment, receiver_chain_id=receiver, edge=edge),
                query.maximum_points_per_stream,
            ),
        ),
        False,
    )


class V10Stub:
    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(299, (), request.path.encode())


class CapturingQueries:
    def __init__(self) -> None:
        self.calls: list[StarlinkPilotConstellationQueryV0_1] = []

    def recording_starlink_pilot_constellation(
        self, query: StarlinkPilotConstellationQueryV0_1
    ) -> RecordingStarlinkPilotConstellationViewV0_1:
        self.calls.append(query)
        return view(query)


def test_v11_defaults_to_bounded_complete_constellation_projection() -> None:
    queries = CapturingQueries()
    application = DashboardJsonApplicationV11(V10Stub(), queries)  # type: ignore[arg-type]

    response = application.handle(
        JsonRequest(
            "GET",
            f"/api/v11/recordings/{RECORDING_ID}/starlink-pilot-constellation",
            {},
        )
    )

    assert response.status == 200
    assert queries.calls == [
        StarlinkPilotConstellationQueryV0_1(
            RECORDING_ID,
            maximum_streams=MAX_CONSTELLATION_QUERY_STREAMS,
            maximum_points_per_stream=MAX_CONSTELLATION_POINTS,
        )
    ]
    payload = json.loads(response.body)
    stream = payload["streams"][0]
    assert stream["evidence_analysis_id"] == "slqam_test"
    assert stream["hard_symbol_accuracy"] == 0.91
    assert stream["rms_evm"] == 0.18
    assert stream["residual_cfo_refinement_hz"] == 4.5
    assert stream["original_point_count"] == 2_400
    assert len(stream["subcarriers"]) == 8
    assert len(stream["display_points"]) == 2_400


def test_v11_parses_selected_segment_receiver_edge_and_resource_bounds() -> None:
    queries = CapturingQueries()
    application = DashboardJsonApplicationV11(V10Stub(), queries)  # type: ignore[arg-type]

    response = application.handle(
        JsonRequest(
            "GET",
            f"/api/v11/recordings/{RECORDING_ID}/starlink-pilot-constellation",
            {
                "segment_ids": "seg_a,seg_b",
                "receiver_chain_ids": "rx_0,rx_1",
                "edges": "upper,lower",
                "maximum_streams": "8",
                "maximum_points_per_stream": "600",
            },
        )
    )

    assert response.status == 200
    query = queries.calls[0]
    assert query.segment_ids == (SegmentId("seg_a"), SegmentId("seg_b"))
    assert query.receiver_chain_ids == (
        ReceiverChainId("rx_0"),
        ReceiverChainId("rx_1"),
    )
    assert query.edges == (StarlinkEdge.UPPER, StarlinkEdge.LOWER)
    assert query.maximum_streams == 8
    assert query.maximum_points_per_stream == 600


@pytest.mark.parametrize(
    "query",
    [
        {"segment_ids": "seg_a,seg_a"},
        {"receiver_chain_ids": "rx_0,rx_0"},
        {"edges": "lower,lower"},
        {"edges": "middle"},
        {"segment_ids": ","},
        {"maximum_streams": "0"},
        {"maximum_streams": str(MAX_CONSTELLATION_QUERY_STREAMS + 1)},
        {"maximum_points_per_stream": str(MAX_CONSTELLATION_POINTS + 1)},
        {"segment_ids": ",".join(f"seg_{index}" for index in range(65))},
        {"segment_ids": "seg_" + "x" * 8_200},
        {"extra": "unsafe"},
    ],
)
def test_v11_rejects_invalid_or_unbounded_filters(query: dict[str, str]) -> None:
    queries = CapturingQueries()
    application = DashboardJsonApplicationV11(V10Stub(), queries)  # type: ignore[arg-type]

    response = application.handle(
        JsonRequest(
            "GET",
            f"/api/v11/recordings/{RECORDING_ID}/starlink-pilot-constellation",
            query,
        )
    )

    assert response.status == 400
    assert json.loads(response.body)["error"]["code"] == "invalid_request"
    assert queries.calls == []


def test_v11_preserves_v10_and_translates_not_found() -> None:
    delegated = DashboardJsonApplicationV11(V10Stub(), CapturingQueries())  # type: ignore[arg-type]
    assert delegated.handle(JsonRequest("GET", "/api/v10/preserved", {})).status == 299

    class MissingQueries:
        def recording_starlink_pilot_constellation(
            self, query: StarlinkPilotConstellationQueryV0_1
        ) -> RecordingStarlinkPilotConstellationViewV0_1:
            raise DashboardNotFound(
                f"constellation for {query.recording_id} was not found"
            )

    missing = DashboardJsonApplicationV11(V10Stub(), MissingQueries())  # type: ignore[arg-type]
    response = missing.handle(
        JsonRequest(
            "GET",
            f"/api/v11/recordings/{RECORDING_ID}/starlink-pilot-constellation",
            {},
        )
    )
    assert response.status == 404
    assert json.loads(response.body)["error"]["code"] == "not_found"


def test_v11_fails_closed_when_a_query_implementation_exceeds_response_bound() -> None:
    class OversizeQueries:
        def recording_starlink_pilot_constellation(
            self, query: StarlinkPilotConstellationQueryV0_1
        ) -> object:
            return {
                "recording_id": query.recording_id,
                "unsafe": "x" * (17 * 1_024 * 1_024),
            }

    application = DashboardJsonApplicationV11(V10Stub(), OversizeQueries())  # type: ignore[arg-type]
    response = application.handle(
        JsonRequest(
            "GET",
            f"/api/v11/recordings/{RECORDING_ID}/starlink-pilot-constellation",
            {},
        )
    )

    assert response.status == 500
    assert json.loads(response.body) == {
        "error": {
            "code": "internal_error",
            "message": "dashboard query failed",
        }
    }
