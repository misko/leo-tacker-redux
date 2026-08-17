from __future__ import annotations

import json
from dataclasses import replace

import pytest

from leo_flow.contracts import dashboard_doppler
from leo_flow.contracts.core import (
    V0_1,
    Digest,
    DigestAlgorithm,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.dashboard_doppler import (
    DopplerAdvancedEvidenceViewV0_1,
    DopplerCandidateAssociationState,
    DopplerCandidatePathAssociationViewV0_1,
    DopplerCandidateViewV0_1,
    DopplerCombEvidenceViewV0_1,
    DopplerOrbitAssociationViewV0_1,
    DopplerProductProvenanceViewV0_1,
    DopplerStationaryControlViewV0_1,
    DopplerTileProvenanceViewV0_1,
    DopplerTrackModel,
    DopplerTrackPointViewV0_1,
    DopplerVisualizationState,
    DopplerWaterfallCoverageViewV0_1,
    DopplerWaterfallLayer,
    DopplerWaterfallTileViewV0_1,
    DopplerWaterfallTimeBinViewV0_1,
    RecordingDopplerVisualizationViewV0_1,
)
from leo_flow.dashboard.api import DashboardJsonApplicationV9, JsonRequest, JsonResponse
from leo_flow.dashboard.repository import (
    DashboardNotFound,
    InMemoryDashboardRepository,
    RecordingDopplerVisualizationProjection,
)

RECORDING_ID = RecordingId("rec_doppler")
SEGMENT_ID = SegmentId("seg_doppler")
RECEIVER_ID = ReceiverChainId("rx_doppler")
DIGEST = Digest(DigestAlgorithm.SHA256, "a" * 64)


def visualization(
    layer: DopplerWaterfallLayer = DopplerWaterfallLayer.RESIDUAL,
    *,
    artifact_id: str = "waterfall_doppler",
) -> RecordingDopplerVisualizationViewV0_1:
    provenance = DopplerProductProvenanceViewV0_1(
        "waterfall-v0-2",
        artifact_id,
        SchemaRef("org.leo-flow.waterfall-bundle", V0_1),
        DIGEST,
        "0.2.0",
        DIGEST,
        "arun_doppler",
        "leo-flow-waterfall",
        "0.2.0",
        "abc123",
        UtcNs(1_000_000_000),
        UtcNs(1_100_000_000),
    )
    tile = DopplerWaterfallTileViewV0_1(
        SEGMENT_ID,
        RECEIVER_ID,
        10_755_000_000.0,
        1_000_000.0,
        8,
        "dbfs-per-bin",
        99.0,
        (-100_000.0, 0.0, 100_000.0),
        DopplerWaterfallCoverageViewV0_1(1, 16, 16, 0, 2, 1.0),
        (
            DopplerWaterfallTimeBinViewV0_1(
                UtcNs(1_000_000_000), (-80.0, -68.0, -79.0)
            ),
            DopplerWaterfallTimeBinViewV0_1(
                UtcNs(2_000_000_000), (-79.0, -63.0, -78.0)
            ),
        ),
    )
    stationary = DopplerStationaryControlViewV0_1(9_000.0, 400.0, 0.955, 12.5, True)
    candidate = DopplerCandidateViewV0_1(
        1,
        7,
        SEGMENT_ID,
        RECEIVER_ID,
        DopplerTrackModel.LINEAR,
        UtcNs(1_000_000_000),
        10_755_000_000.0,
        25_000.0,
        0.0,
        400.0,
        380.0,
        2,
        13.5,
        -61.0,
        1.0,
        0.0,
        0,
        22.0,
        stationary,
        (
            DopplerTrackPointViewV0_1(
                UtcNs(1_000_000_000), 10_754_975_000.0, -64.0, 12.0, False
            ),
            DopplerTrackPointViewV0_1(
                UtcNs(2_000_000_000), 10_755_000_000.0, -61.0, 15.0, False
            ),
        ),
    )
    advanced = DopplerAdvancedEvidenceViewV0_1(
        1,
        SEGMENT_ID,
        RECEIVER_ID,
        0.25,
        9.0,
        2.0,
        1.5,
        (1.0, 1.2, 0.9),
        comb=DopplerCombEvidenceViewV0_1(8.0, 7.5, 1.0),
        orbit_association=DopplerOrbitAssociationViewV0_1(
            "STARLINK-TEST", 0.5, 0.2, 1.2, 4.0, 3.5, True
        ),
        drift_rate_hz_s=25_000.0,
        spectral_peak_excess_reference="local-temporal-median-db",
        source_input_digest=DIGEST,
        candidate_path_digest=DIGEST,
        association=DopplerCandidatePathAssociationViewV0_1(
            DopplerCandidateAssociationState.MATCHED_BASIC_CANDIDATE,
            DIGEST,
            1,
            2,
            1.0,
            125.0,
            250.0,
        ),
    )
    return RecordingDopplerVisualizationViewV0_1(
        SchemaRef(RecordingDopplerVisualizationViewV0_1.SCHEMA_ID, V0_1),
        RECORDING_ID,
        DopplerVisualizationState.COMPLETE,
        layer,
        True,
        None,
        provenance,
        (
            DopplerTileProvenanceViewV0_1(
                SEGMENT_ID,
                RECEIVER_ID,
                DopplerProductProvenanceViewV0_1(
                    "blind-doppler",
                    "blind_doppler_bundle",
                    SchemaRef("org.leo-flow.blind-doppler-bundle", V0_1),
                    DIGEST,
                    "0.1.0",
                    DIGEST,
                ),
                DopplerProductProvenanceViewV0_1(
                    "advanced-doppler",
                    "advanced_doppler_bundle",
                    SchemaRef("org.leo-flow.doppler-evidence-bundle", V0_1),
                    DIGEST,
                    "0.1.0",
                    DIGEST,
                ),
            ),
        ),
        (tile,),
        (candidate,),
        (advanced,),
        (RecordingDopplerVisualizationViewV0_1.CANDIDATE_WARNING,),
        (),
    )


class V8Stub:
    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(299, (), request.path.encode())


class CapturingQueries:
    def __init__(self) -> None:
        self.calls: list[tuple[RecordingId, DopplerWaterfallLayer]] = []

    def recording_doppler_visualization(
        self, recording_id: RecordingId, layer: DopplerWaterfallLayer
    ) -> RecordingDopplerVisualizationViewV0_1:
        self.calls.append((recording_id, layer))
        return visualization(layer)


def test_v9_defaults_to_residual_and_projects_exactly_one_layer() -> None:
    queries = CapturingQueries()
    application = DashboardJsonApplicationV9(V8Stub(), queries)  # type: ignore[arg-type]

    response = application.handle(
        JsonRequest(
            "GET", f"/api/v9/recordings/{RECORDING_ID}/doppler-visualization", {}
        )
    )

    assert response.status == 200
    assert queries.calls == [(RECORDING_ID, DopplerWaterfallLayer.RESIDUAL)]
    payload = json.loads(response.body)
    assert payload["selected_layer"] == "residual"
    assert payload["candidate_only"] is True
    assert payload["calibrated_detection_count"] is None
    assert payload["tiles"][0]["time_bins"][0]["power_db"] == [
        -80.0,
        -68.0,
        -79.0,
    ]
    assert "average_power_db" not in response.body.decode()
    assert "temporal_median_residual_db" not in response.body.decode()
    assert "high_percentile_power_db" not in response.body.decode()
    assert payload["advanced_evidence"][0]["orbit_association"]["name"] == (
        "STARLINK-TEST"
    )
    assert b"locator" not in response.body and b"/home/" not in response.body


@pytest.mark.parametrize(
    "selector, expected",
    [
        ("average", DopplerWaterfallLayer.AVERAGE),
        ("residual", DopplerWaterfallLayer.RESIDUAL),
        ("high-percentile", DopplerWaterfallLayer.HIGH_PERCENTILE),
    ],
)
def test_v9_accepts_each_bounded_layer_selector(
    selector: str, expected: DopplerWaterfallLayer
) -> None:
    queries = CapturingQueries()
    application = DashboardJsonApplicationV9(V8Stub(), queries)  # type: ignore[arg-type]

    response = application.handle(
        JsonRequest(
            "GET",
            f"/api/v9/recordings/{RECORDING_ID}/doppler-visualization",
            {"layer": selector},
        )
    )

    assert response.status == 200
    assert queries.calls == [(RECORDING_ID, expected)]


def test_v9_rejects_unknown_layers_and_preserves_earlier_routes() -> None:
    queries = CapturingQueries()
    application = DashboardJsonApplicationV9(V8Stub(), queries)  # type: ignore[arg-type]

    invalid = application.handle(
        JsonRequest(
            "GET",
            f"/api/v9/recordings/{RECORDING_ID}/doppler-visualization",
            {"layer": "all"},
        )
    )
    mutation = application.handle(
        JsonRequest(
            "POST", f"/api/v9/recordings/{RECORDING_ID}/doppler-visualization", {}
        )
    )
    unknown = application.handle(
        JsonRequest("GET", f"/api/v9/recordings/{RECORDING_ID}/raw-iq", {})
    )
    earlier = application.handle(JsonRequest("GET", "/api/v8/score-distributions", {}))

    assert invalid.status == 400
    assert json.loads(invalid.body)["error"]["code"] == "invalid_request"
    assert queries.calls == []
    assert mutation.status == 405
    assert unknown.status == 404
    assert earlier.status == 299 and earlier.body == b"/api/v8/score-distributions"


def test_in_memory_repository_selects_latest_projection_for_requested_layer() -> None:
    average = visualization(DopplerWaterfallLayer.AVERAGE, artifact_id="average_old")
    average_latest = visualization(
        DopplerWaterfallLayer.AVERAGE, artifact_id="average_latest"
    )
    residual = visualization(DopplerWaterfallLayer.RESIDUAL, artifact_id="residual")
    repository = InMemoryDashboardRepository(
        recording_doppler_visualizations=(
            RecordingDopplerVisualizationProjection(average, 1),
            RecordingDopplerVisualizationProjection(residual, 2),
            RecordingDopplerVisualizationProjection(average_latest, 3),
        )
    )

    selected = repository.recording_doppler_visualization(
        RECORDING_ID, DopplerWaterfallLayer.AVERAGE
    )

    assert selected.waterfall_provenance is not None
    assert selected.waterfall_provenance.artifact_id == "average_latest"
    with pytest.raises(DashboardNotFound):
        repository.recording_doppler_visualization(
            RecordingId("rec_missing"), DopplerWaterfallLayer.RESIDUAL
        )


def test_contract_requires_candidate_only_warning() -> None:
    value = visualization()
    with pytest.raises(ValueError, match="candidate-only warning"):
        RecordingDopplerVisualizationViewV0_1(
            value.schema,
            value.recording_id,
            value.state,
            value.selected_layer,
            True,
            None,
            value.waterfall_provenance,
            value.doppler_provenance,
            value.tiles,
            value.candidates,
            value.advanced_evidence,
            (),
            (),
        )


def test_contract_keeps_basic_and_advanced_provenance_per_analyzed_tile() -> None:
    first = visualization()
    second_segment = SegmentId("seg_doppler_b")
    second_receiver = ReceiverChainId("rx_doppler_b")
    second_tile = replace(
        first.tiles[0],
        segment_id=second_segment,
        receiver_chain_id=second_receiver,
    )
    second_candidate = replace(
        first.candidates[0],
        segment_id=second_segment,
        receiver_chain_id=second_receiver,
    )
    second_advanced = replace(
        first.advanced_evidence[0],
        segment_id=second_segment,
        receiver_chain_id=second_receiver,
    )
    first_provenance = first.doppler_provenance[0]
    assert first_provenance.advanced is not None
    second_provenance = DopplerTileProvenanceViewV0_1(
        second_segment,
        second_receiver,
        replace(first_provenance.basic, artifact_id="blind_doppler_bundle_b"),
        replace(first_provenance.advanced, artifact_id="advanced_doppler_bundle_b"),
    )

    multi = replace(
        first,
        tiles=(*first.tiles, second_tile),
        candidates=(*first.candidates, second_candidate),
        advanced_evidence=(*first.advanced_evidence, second_advanced),
        doppler_provenance=(*first.doppler_provenance, second_provenance),
    )

    assert [item.basic.artifact_id for item in multi.doppler_provenance] == [
        "blind_doppler_bundle",
        "blind_doppler_bundle_b",
    ]
    assert [item.advanced.artifact_id for item in multi.doppler_provenance] == [
        "advanced_doppler_bundle",
        "advanced_doppler_bundle_b",
    ]
    assert [candidate.rank for candidate in multi.candidates] == [1, 1]

    with pytest.raises(ValueError, match="identify every analyzed tile"):
        replace(multi, doppler_provenance=multi.doppler_provenance[:1])


def test_contract_supports_an_unmatched_advanced_path_without_truth_leakage() -> None:
    value = visualization()
    advanced = replace(
        value.advanced_evidence[0],
        candidate_rank=None,
        association=DopplerCandidatePathAssociationViewV0_1(
            DopplerCandidateAssociationState.ADVANCED_PATH_ONLY,
            DIGEST,
            None,
            0,
            0.0,
            None,
            None,
        ),
        orbit_association=None,
    )

    projected = replace(value, advanced_evidence=(advanced,))

    assert projected.advanced_evidence[0].candidate_rank is None
    assert projected.calibrated_detection_count is None


def test_contract_rejects_mismatched_candidate_path_association() -> None:
    value = visualization()
    different_digest = Digest(DigestAlgorithm.SHA256, "b" * 64)

    with pytest.raises(ValueError, match="different candidate path"):
        replace(
            value.advanced_evidence[0],
            association=replace(
                value.advanced_evidence[0].association,
                candidate_path_digest=different_digest,
            ),
        )


def test_contract_enforces_response_cell_and_byte_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = visualization()
    pixels = sum(
        len(tile.time_bins) * len(tile.frequency_bin_offsets_hz) for tile in value.tiles
    )
    monkeypatch.setattr(
        dashboard_doppler,
        "MAX_DASHBOARD_DOPPLER_PIXELS",
        pixels - 1,
    )
    with pytest.raises(ValueError, match="pixels exceed"):
        replace(value)

    monkeypatch.setattr(
        dashboard_doppler,
        "MAX_DASHBOARD_DOPPLER_PIXELS",
        pixels,
    )
    monkeypatch.setattr(dashboard_doppler, "MAX_DASHBOARD_DOPPLER_JSON_BYTES", 1)
    with pytest.raises(ValueError, match="JSON exceeds"):
        replace(value)
