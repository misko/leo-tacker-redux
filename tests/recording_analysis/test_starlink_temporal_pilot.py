from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import cast

import pytest

from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
    StarlinkDetectorSuiteV0_2,
    StarlinkInjectionCaseV0_2,
    starlink_detector_suite_algorithm_ref_v0_2,
    starlink_detector_suite_config_ref_v0_2,
    synthesize_starlink_injection_v0_2,
)
from leo_flow.analysis.recording.starlink_suite_recording import (
    ExactStarlinkDetectorSuiteRecordingAnalyzerV0_2,
)
from leo_flow.analysis.recording.starlink_surrogate_null import (
    starlink_search_grid_v0_1,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.analysis.recording.starlink_temporal_pilot_codec import (
    MalformedStarlinkTemporalPilotError,
    decode_starlink_temporal_pilot,
    encode_starlink_temporal_pilot,
)
from leo_flow.analysis.recording.starlink_temporal_pilot_persistence import (
    DurableRecordingStarlinkTemporalPilotQueryV0_1,
    _extrema_preserving,
)
from leo_flow.analysis.recording.starlink_temporal_pilot_recording import (
    ExactStarlinkTemporalPilotRecordingAnalyzerV0_1,
    temporal_probe_starts_v0_1,
)
from leo_flow.contracts.core import Digest, RadioId, ReceiverChainId, SchemaRef
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import REPORT_METHOD_ORDER, V0_2
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteRecordingBundleV0_2,
    StarlinkDetectorSuiteRequestV0_2,
    StarlinkSuiteStreamSelectionV0_2,
)
from leo_flow.contracts.starlink_temporal_pilot import (
    StarlinkTemporalPilotProductRefV0_1,
    StarlinkTemporalPilotQueryV0_1,
    StarlinkTemporalPilotRecordingBundleV0_1,
    StarlinkTemporalProbePlanV0_1,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.services.starlink_temporal_pilot_analysis import (
    temporal_pilot_request_v0_1,
)
from leo_flow.storage.ports import RecordingView

from .fakes import FakeRecordingView, SegmentFixture, execution_context, make_view

SAMPLE_RATE_HZ = 2_500_000.0


def _ci16_pair(values: tuple[complex, ...]) -> bytes:
    result = bytearray()
    for value in values:
        i = round(max(-2048, min(2047, value.real * 512)))
        q = round(max(-2048, min(2047, value.imag * 512)))
        result.extend(int(i).to_bytes(2, "little", signed=True))
        result.extend(int(q).to_bytes(2, "little", signed=True))
        result.extend(int(-q).to_bytes(2, "little", signed=True))
        result.extend(int(i).to_bytes(2, "little", signed=True))
    return bytes(result)


@pytest.fixture(scope="module")
def temporal_result() -> tuple[
    FakeRecordingView, StarlinkTemporalPilotRecordingBundleV0_1
]:
    config = StarlinkDetectorSuiteConfigV0_2((0, 3), (0.0, 1_000.0), (0.0, 50.0))
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    values = synthesize_starlink_injection_v0_2(
        templates,
        StarlinkInjectionCaseV0_2(
            "temporal-middle",
            91,
            7_500,
            1.2,
            0.05,
            3,
            1_000.0,
            0.0,
            (0, 1),
        ),
    )
    # Three distinct temporal positions; the middle contains the exact injection
    # and the flanks are deterministic zero/null controls.
    data = _ci16_pair(tuple(0j for _ in values) + values + tuple(0j for _ in values))
    original, recording_ref = make_view(SegmentFixture(data, int(SAMPLE_RATE_HZ)))
    segment = original.manifest.segments[0]
    tagged = replace(
        segment,
        requested=replace(
            segment.requested,
            tags=(("channel", "4"), ("edge", "lower")),
        ),
    )
    view = FakeRecordingView(
        replace(original.manifest, segments=(tagged,)), {segment.segment_id: data}
    )
    source_request = StarlinkDetectorSuiteRequestV0_2(
        SchemaRef(StarlinkDetectorSuiteRequestV0_2.SCHEMA_ID, V0_2),
        recording_ref.recording_id,
        recording_ref,
        starlink_detector_suite_algorithm_ref_v0_2(),
        starlink_detector_suite_config_ref_v0_2(config),
        (
            StarlinkSuiteStreamSelectionV0_2(
                segment.segment_id,
                ReceiverChainId("rx_0"),
                StarlinkEdge.LOWER,
                templates.exact_ref,
                templates.conditioned_control_ref,
                len(values),
            ),
        ),
        SchemaRef(StarlinkDetectorSuiteRecordingBundleV0_2.SCHEMA_ID, V0_2),
    )
    source = ExactStarlinkDetectorSuiteRecordingAnalyzerV0_2(
        StarlinkDetectorSuiteV0_2(config, execution_context())
    ).analyze_starlink_suite(cast(RecordingView, view), source_request)
    request = temporal_pilot_request_v0_1(
        cast(RecordingView, view),
        source_request,
        source,
        starlink_search_grid_v0_1(config),
        stride_seconds=1,
        maximum_probe_count=3,
    )
    # Override only the additive temporal plan to select the three fixture windows.
    request = replace(
        request,
        plan=StarlinkTemporalProbePlanV0_1(len(values), len(values), 3, 4),
    )
    result = ExactStarlinkTemporalPilotRecordingAnalyzerV0_1(
        config, execution_context()
    ).analyze_temporal_pilot(cast(RecordingView, view), request)
    return view, result


def test_probe_plan_includes_beginning_middle_and_exact_tail_without_prefix_bias() -> (
    None
):
    assert temporal_probe_starts_v0_1(50_000_000, 20_000, 12_500_000, 8) == (
        0,
        12_500_000,
        25_000_000,
        37_500_000,
        49_980_000,
    )
    assert temporal_probe_starts_v0_1(100, 20, 10, 3) == (0, 40, 80)


def test_temporal_search_covers_all_positions_and_methods_with_exact_coverage(
    temporal_result: tuple[FakeRecordingView, StarlinkTemporalPilotRecordingBundleV0_1],
) -> None:
    view, result = temporal_result
    stream = result.streams[0]
    assert stream.probe_starts == (0, 7_500, 15_000)
    assert tuple((item.probe_index, item.method) for item in stream.points) == tuple(
        (probe, method) for probe in range(3) for method in REPORT_METHOD_ORDER
    )
    assert stream.analyzed_sample_count == stream.segment_sample_count
    assert stream.coverage_fraction == 1.0
    # One interleaved read per temporal window, not one reread per method/pattern.
    assert view.calls[-3:] == [
        (stream.segment_id, 0, 7_500),
        (stream.segment_id, 7_500, 15_000),
        (stream.segment_id, 15_000, 22_500),
    ]
    glrt = [item for item in stream.points if item.method.value == "glrt-32"]
    winner = max(glrt, key=lambda item: item.qin.score)
    assert winner.probe_index == 1
    assert winner.qin.winning_epoch_sample == 3
    assert winner.qin.winning_coarse_cfo_hz == 1_000.0
    assert winner.qin_minus_max_surrogate > 0.8
    assert all(len(item.surrogates) == 4 for item in stream.points)


def test_dwell_max_applies_identical_time_look_elsewhere_to_each_pattern(
    temporal_result: tuple[FakeRecordingView, StarlinkTemporalPilotRecordingBundleV0_1],
) -> None:
    _, result = temporal_result
    stream = result.streams[0]
    for summary in stream.dwell_summaries:
        points = [item for item in stream.points if item.method is summary.method]
        assert summary.qin_maximum == max(item.qin.score for item in points)
        assert summary.surrogate_maxima == tuple(
            max(item.surrogates[index].winner.score for item in points)
            for index in range(4)
        )
        assert summary.candidate_occupancy_fraction == pytest.approx(
            sum(item.qin_minus_max_surrogate > 0 for item in points) / 3
        )


def test_temporal_codec_is_canonical_deterministic_and_rejects_trailing_bytes(
    temporal_result: tuple[FakeRecordingView, StarlinkTemporalPilotRecordingBundleV0_1],
) -> None:
    _, result = temporal_result
    payload = encode_starlink_temporal_pilot(result)
    assert decode_starlink_temporal_pilot(payload) == result
    assert (
        encode_starlink_temporal_pilot(decode_starlink_temporal_pilot(payload))
        == payload
    )
    with pytest.raises(MalformedStarlinkTemporalPilotError):
        decode_starlink_temporal_pilot(payload + b"\n")


def test_plan_exposes_overlap_without_claiming_independence() -> None:
    plan = StarlinkTemporalProbePlanV0_1(20_000, 10_000, 8, 4)
    assert plan.overlap_fraction == 0.5


def test_extrema_decimation_preserves_first_last_and_qin_peak(
    temporal_result: tuple[FakeRecordingView, StarlinkTemporalPilotRecordingBundleV0_1],
) -> None:
    _, result = temporal_result
    points = result.streams[0].points
    selected = _extrema_preserving(points, 5)
    assert points[0] in selected
    assert points[-1] in selected
    assert max(points, key=lambda item: item.qin.score) in selected


def test_query_never_pools_or_crosses_radio_and_receiver_filters(
    temporal_result: tuple[FakeRecordingView, StarlinkTemporalPilotRecordingBundleV0_1],
) -> None:
    _, original = temporal_result
    first = original.streams[0]
    second = replace(
        first,
        radio_id=RadioId("radio_21"),
        receiver_chain_id=ReceiverChainId("rx_1"),
    )
    bundle = replace(original, streams=(first, second))
    ref = StarlinkTemporalPilotProductRefV0_1(
        bundle.analysis_id,
        bundle.recording_id,
        ObjectRef(
            Digest.sha256(b"temporal-query"),
            1,
            "application/json",
            "starlink-temporal-pilot-v0.1",
            "sha256/temporal-query",
        ),
    )

    class Catalog:
        def latest_starlink_temporal_pilot(self, recording_id):
            assert recording_id == bundle.recording_id
            return ref

    class Store:
        @contextmanager
        def open(
            self, product_ref
        ) -> Iterator[StarlinkTemporalPilotRecordingBundleV0_1]:
            assert product_ref == ref
            yield bundle

    query = DurableRecordingStarlinkTemporalPilotQueryV0_1(  # type: ignore[arg-type]
        Store(), Catalog()
    )
    radio_view = query.recording_starlink_temporal_pilot(
        StarlinkTemporalPilotQueryV0_1(bundle.recording_id, radio_ids=(first.radio_id,))
    )
    receiver_view = query.recording_starlink_temporal_pilot(
        StarlinkTemporalPilotQueryV0_1(
            bundle.recording_id,
            receiver_chain_ids=(ReceiverChainId("rx_1"),),
        )
    )
    assert [(item.radio_id, item.receiver_chain_id) for item in radio_view.streams] == [
        (first.radio_id, first.receiver_chain_id)
    ]
    assert [
        (item.radio_id, item.receiver_chain_id) for item in receiver_view.streams
    ] == [(second.radio_id, second.receiver_chain_id)]
