from __future__ import annotations

from leo_flow.adapters.dashboard_temporal_pilot import _stratum
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod
from tests.recording_analysis.test_starlink_temporal_pilot import (
    temporal_result as _temporal_result_fixture,
)


def test_temporal_aggregate_keeps_radio_receiver_edge_and_per_probe_semantics() -> None:
    _view, bundle = _temporal_result_fixture.__wrapped__()
    stream = bundle.streams[0]
    points = tuple(
        item for item in stream.points if item.method is StarlinkDetectorMethod.GLRT_32
    )
    result = _stratum(
        (
            StarlinkDetectorMethod.GLRT_32.value,
            str(stream.radio_id),
            str(stream.receiver_chain_id),
            stream.edge.value,
        ),
        [(str(bundle.recording_id), stream, points)],
    )
    assert result.recording_count == 1
    assert result.probe_count == len(points)
    assert result.mean_probe_maximum_qin_score == sum(
        item.qin.score for item in points
    ) / len(points)
    assert result.mean_union_coverage_fraction == stream.coverage_fraction
    assert result.radio_id == str(stream.radio_id)
    assert result.receiver_chain_id == str(stream.receiver_chain_id)
