from __future__ import annotations

import json
from pathlib import Path

import pytest

from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    Provenance,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import (
    REPORT_METHOD_ORDER,
    StarlinkDetectorMethod,
)
from leo_flow.contracts.starlink_surrogate_null import (
    StarlinkSearchPatternRole,
    StarlinkSearchPatternV0_1,
)
from leo_flow.contracts.starlink_surrogate_null_pipeline import (
    MAXIMUM_SURROGATE_NULL_QUERY_ROWS,
    RecordingStarlinkSurrogateNullViewV0_1,
    StarlinkSurrogateNullMethodAggregateV0_1,
    StarlinkSurrogateNullMethodRowV0_1,
    StarlinkSurrogateNullQueryV0_1,
    StarlinkSurrogateNullRecordingState,
)
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV10,
    JsonRequest,
    JsonResponse,
)

RECORDING_ID = RecordingId("rec_surrogate")


def _digest(value: str) -> Digest:
    return Digest.sha256(value.encode())


def _artifact(value: str) -> ArtifactRef:
    return ArtifactRef(value, _digest(value), SchemaRef("org.leo-flow.test-artifact"))


def _pattern(index: int, edge: StarlinkEdge) -> StarlinkSearchPatternV0_1:
    return StarlinkSearchPatternV0_1(
        SchemaRef(StarlinkSearchPatternV0_1.SCHEMA_ID, V0_1),
        f"surrogate_{index}",
        StarlinkSearchPatternRole.PRECOMMITTED_SURROGATE,
        _artifact(f"surrogate_template_{index}"),
        edge,
        tuple(range(528, 536))
        if edge is StarlinkEdge.LOWER
        else tuple(range(488, 496)),
        2,
        301,
        750.0,
        2_500_000.0,
        3_333,
        300.0,
        _digest(f"states-{index}"),
        "precommitted-qpsk-v0-1",
        10_000 + index,
        index,
        True,
    )


def _provenance() -> Provenance:
    return Provenance(
        "surrogate-null-analyzer",
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


def view(
    query: StarlinkSurrogateNullQueryV0_1,
) -> RecordingStarlinkSurrogateNullViewV0_1:
    radio = query.radio_ids[0] if query.radio_ids else RadioId("radio_surrogate")
    channel = query.channel_numbers[0] if query.channel_numbers else 4
    edge = query.edges[0] if query.edges else StarlinkEdge.LOWER
    start = query.interval_start_utc_ns or UtcNs(1_000_000_000)
    stop = query.interval_stop_utc_ns or UtcNs(2_000_000_000)
    rows = tuple(
        StarlinkSurrogateNullMethodRowV0_1(
            radio,
            SegmentId("seg_surrogate"),
            ReceiverChainId("rx_surrogate"),
            channel,
            edge,
            start,
            stop,
            method,
            0.8,
            (0.1, 0.2, 0.9, 0.3),
            0.4,
            123,
            1_250.0,
            -75.0,
            tuple(_pattern(index, edge) for index in range(4)),
            _provenance(),
            None,
            None,
        )
        for method in query.methods
    )
    aggregates = tuple(
        StarlinkSurrogateNullMethodAggregateV0_1(
            method,
            1,
            0.8,
            0.375,
            0.4,
            0,
            "finite-paired-upper-tail-rank-not-calibrated-p-value",
        )
        for method in query.methods
    )
    return RecordingStarlinkSurrogateNullViewV0_1(
        SchemaRef(RecordingStarlinkSurrogateNullViewV0_1.SCHEMA_ID, V0_1),
        query.recording_id,
        StarlinkSurrogateNullRecordingState.CANDIDATES,
        _artifact("slsnullrec_test"),
        query,
        len(rows),
        rows,
        aggregates,
        None,
        (
            "candidate-evidence-not-detection",
            "finite-rank-not-calibrated-p-value",
        ),
    )


class V9Stub:
    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(299, (), request.path.encode())


class CapturingQueries:
    def __init__(self) -> None:
        self.calls: list[StarlinkSurrogateNullQueryV0_1] = []

    def recording_starlink_surrogate_null(
        self, query: StarlinkSurrogateNullQueryV0_1
    ) -> RecordingStarlinkSurrogateNullViewV0_1:
        self.calls.append(query)
        return view(query)


def test_v10_defaults_to_the_bounded_complete_method_set() -> None:
    queries = CapturingQueries()
    application = DashboardJsonApplicationV10(V9Stub(), queries)  # type: ignore[arg-type]

    response = application.handle(
        JsonRequest(
            "GET",
            f"/api/v10/recordings/{RECORDING_ID}/starlink-surrogate-null",
            {},
        )
    )

    assert response.status == 200
    assert len(queries.calls) == 1
    query = queries.calls[0]
    assert query.methods == REPORT_METHOD_ORDER
    assert query.maximum_rows == MAXIMUM_SURROGATE_NULL_QUERY_ROWS
    payload = json.loads(response.body)
    assert payload["calibrated_detection_count"] is None
    assert payload["rows"][0]["qin_score"] == 0.8
    assert payload["rows"][0]["surrogate_scores"] == [0.1, 0.2, 0.9, 0.3]
    assert len(payload["rows"][0]["surrogate_patterns"]) == 4
    assert payload["rows"][0]["provenance"]["producer_name"] == (
        "surrogate-null-analyzer"
    )


def test_v10_parses_all_bounded_recording_filters() -> None:
    queries = CapturingQueries()
    application = DashboardJsonApplicationV10(V9Stub(), queries)  # type: ignore[arg-type]

    response = application.handle(
        JsonRequest(
            "GET",
            f"/api/v10/recordings/{RECORDING_ID}/starlink-surrogate-null",
            {
                "methods": "glrt-32,anchor-8",
                "radio_ids": "radio_21,radio_20",
                "channel_numbers": "4,1",
                "edges": "upper,lower",
                "interval_start_utc_ns": "1000000000",
                "interval_stop_utc_ns": "2000000000",
                "maximum_rows": "17",
            },
        )
    )

    assert response.status == 200
    query = queries.calls[0]
    assert query.methods == (
        StarlinkDetectorMethod.ANCHOR_8,
        StarlinkDetectorMethod.GLRT_32,
    )
    assert query.radio_ids == (RadioId("radio_21"), RadioId("radio_20"))
    assert query.channel_numbers == (1, 4)
    assert query.edges == (StarlinkEdge.UPPER, StarlinkEdge.LOWER)
    assert query.interval_start_utc_ns == 1_000_000_000
    assert query.interval_stop_utc_ns == 2_000_000_000
    assert query.maximum_rows == 17


@pytest.mark.parametrize(
    "query",
    [
        {"methods": "unknown"},
        {"methods": "glrt-32,glrt-32"},
        {"radio_ids": ".20"},
        {"channel_numbers": "0"},
        {"edges": "middle"},
        {"interval_start_utc_ns": "2", "interval_stop_utc_ns": "1"},
        {"maximum_rows": str(MAXIMUM_SURROGATE_NULL_QUERY_ROWS + 1)},
        {"radio_ids": ",".join(f"radio_{index}" for index in range(65))},
        {"radio_ids": "radio_" + ("x" * 8_200)},
        {"extra": "unsafe"},
    ],
)
def test_v10_rejects_invalid_or_unbounded_filters(query: dict[str, str]) -> None:
    queries = CapturingQueries()
    application = DashboardJsonApplicationV10(V9Stub(), queries)  # type: ignore[arg-type]

    response = application.handle(
        JsonRequest(
            "GET",
            f"/api/v10/recordings/{RECORDING_ID}/starlink-surrogate-null",
            query,
        )
    )

    assert response.status == 400
    assert json.loads(response.body)["error"]["code"] == "invalid_request"
    assert queries.calls == []


def test_v10_preserves_v9_and_rejects_mutation_or_unknown_v10_routes() -> None:
    queries = CapturingQueries()
    application = DashboardJsonApplicationV10(V9Stub(), queries)  # type: ignore[arg-type]

    mutation = application.handle(
        JsonRequest(
            "POST",
            f"/api/v10/recordings/{RECORDING_ID}/starlink-surrogate-null",
            {},
        )
    )
    unknown = application.handle(
        JsonRequest("GET", f"/api/v10/recordings/{RECORDING_ID}/raw-iq", {})
    )
    earlier = application.handle(
        JsonRequest(
            "GET", f"/api/v9/recordings/{RECORDING_ID}/doppler-visualization", {}
        )
    )

    assert mutation.status == 405
    assert unknown.status == 404
    assert earlier.status == 299
    assert earlier.body.endswith(b"doppler-visualization")
    assert queries.calls == []


def test_recording_ui_declares_safe_surrogate_controls_and_pending_state() -> None:
    static = Path("src/leo_flow/dashboard/static")
    html = (static / "recording.html").read_text()
    javascript = (static / "recording-detail.js").read_text()

    for control in (
        "surrogate-method",
        "surrogate-radio",
        "surrogate-channel",
        "surrogate-edge",
        "surrogate-start",
        "surrogate-stop",
    ):
        assert f'id="{control}"' in html
    assert 'id="surrogate-state"' in html
    assert 'data-state="pending"' in html
    assert "not a calibrated p-value" in html
    assert "candidate evidence is not a Starlink detection" in html
    assert "/api/v10/recordings/${encodeURIComponent(recordingId)}" in javascript
    assert "starlink-surrogate-null" in javascript
    assert "innerHTML" not in javascript
