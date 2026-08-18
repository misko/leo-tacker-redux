from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
    StarlinkInjectionCaseV0_2,
    synthesize_starlink_injection_v0_2,
)
from leo_flow.analysis.recording.starlink_full_dwell_response import (
    ExactStarlinkFullDwellResponseAnalyzerV0_1,
    covering_window_starts_v0_1,
)
from leo_flow.analysis.recording.starlink_full_dwell_response_codec import (
    MalformedStarlinkFullDwellResponseError,
    decode_starlink_full_dwell_response,
    encode_starlink_full_dwell_response,
)
from leo_flow.analysis.recording.starlink_surrogate_null import (
    starlink_search_grid_v0_1,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.core import ArtifactRef, Digest, ReceiverChainId, SchemaRef
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import REPORT_METHOD_ORDER
from leo_flow.contracts.starlink_full_dwell_response import (
    MAXIMUM_EXACT_WINDOWS_PER_STREAM,
    V0_1,
    StarlinkFullDwellPlanV0_1,
    StarlinkFullDwellRequestV0_1,
    StarlinkFullDwellResponseBundleV0_1,
    StarlinkFullDwellStreamSelectionV0_1,
    StarlinkWindowTier,
)
from leo_flow.storage.ports import RecordingView

from .fakes import FakeRecordingView, SegmentFixture, execution_context, make_view

SAMPLE_RATE_HZ = 2_500_000.0


def _paired_ci16(rx0: tuple[complex, ...], rx1: tuple[complex, ...]) -> bytes:
    result = bytearray()
    for left, right in zip(rx0, rx1, strict=True):
        for value in (left, right):
            i = round(max(-2048, min(2047, value.real * 512)))
            q = round(max(-2048, min(2047, value.imag * 512)))
            result.extend(int(i).to_bytes(2, "little", signed=True))
            result.extend(int(q).to_bytes(2, "little", signed=True))
    return bytes(result)


@pytest.fixture(scope="module")
def full_dwell_result():
    config = StarlinkDetectorSuiteConfigV0_2((0, 3), (0.0, 1_000.0), (0.0, 50.0))
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    injection = synthesize_starlink_injection_v0_2(
        templates,
        StarlinkInjectionCaseV0_2(
            "full-dwell-middle", 41, 7_500, 1.2, 0.04, 3, 1_000.0, 0.0, (0, 1)
        ),
    )
    zero = tuple(0j for _ in injection)
    rx0 = zero + injection + zero
    # A power-only end burst makes RX selection demonstrably independent while
    # retaining the same immutable interleaved recording.
    rx1 = zero + zero + tuple(complex(0.7, -0.2) for _ in injection)
    data = _paired_ci16(rx0, rx1)
    original, recording_ref = make_view(SegmentFixture(data, int(SAMPLE_RATE_HZ)))
    segment = original.manifest.segments[0]
    tagged = replace(
        segment,
        requested=replace(
            segment.requested, tags=(("channel", "4"), ("edge", "lower"))
        ),
    )
    view = FakeRecordingView(
        replace(original.manifest, segments=(tagged,)), {segment.segment_id: data}
    )
    plan = StarlinkFullDwellPlanV0_1(7_500, 7_500, 7_500, 16, 1, 4)
    selections = tuple(
        StarlinkFullDwellStreamSelectionV0_1(
            segment.segment_id,
            receiver,
            StarlinkEdge.LOWER,
            SAMPLE_RATE_HZ,
            segment.sample_count,
        )
        for receiver in (ReceiverChainId("rx_0"), ReceiverChainId("rx_1"))
    )
    request = StarlinkFullDwellRequestV0_1(
        SchemaRef(StarlinkFullDwellRequestV0_1.SCHEMA_ID, V0_1),
        recording_ref.recording_id,
        recording_ref,
        ArtifactRef("source-suite", Digest.sha256(b"suite"), None),
        Digest.sha256(b"source-request"),
        starlink_search_grid_v0_1(config),
        plan,
        selections,
        SchemaRef(StarlinkFullDwellResponseBundleV0_1.SCHEMA_ID, V0_1),
    )
    result = ExactStarlinkFullDwellResponseAnalyzerV0_1(
        config, execution_context()
    ).analyze_full_dwell(cast(RecordingView, view), request)
    return view, request, result


def test_covering_windows_include_begin_middle_tail_and_overlap_without_aliases() -> (
    None
):
    assert covering_window_starts_v0_1(100, 40, 30) == (0, 30, 60)
    assert covering_window_starts_v0_1(101, 40, 30) == (0, 30, 60, 61)
    with pytest.raises(ValueError, match="gaps"):
        covering_window_starts_v0_1(100, 20, 21)


def test_full_dwell_exactly_covers_every_method_and_preserves_search_maxima(
    full_dwell_result,
) -> None:
    _, _, result = full_dwell_result
    for stream in result.streams:
        assert stream.prescreen_covered_sample_count == stream.segment_sample_count
        assert stream.prescreen_coverage_fraction == 1.0
        assert tuple(item.start_sample for item in stream.prescreen_windows) == (
            0,
            7_500,
            15_000,
        )
        assert stream.exact_covered_sample_count == 7_500
        assert stream.exact_coverage_fraction == pytest.approx(1 / 3)
        assert tuple(
            (point.tier, point.window_index, point.method) for point in stream.points
        ) == tuple(
            (StarlinkWindowTier.EXACT_REFINEMENT, index, method)
            for index in range(len(stream.exact_window_starts))
            for method in REPORT_METHOD_ORDER
        )
        assert all(
            point.qin.aggregation == "maximum-over-declared-epoch-cfo-cells"
            for point in stream.points
        )
        assert all(
            point.qin.winning_epoch_sample_in_segment
            == point.start_sample + point.qin.winning_epoch_sample_in_window
            for point in stream.points
        )


def test_pattern_blind_refinement_is_per_receiver_and_wrong_patterns_are_controls(
    full_dwell_result,
) -> None:
    _, _, result = full_dwell_result
    by_rx = {stream.receiver_chain_id: stream for stream in result.streams}
    assert (
        by_rx[ReceiverChainId("rx_0")].exact_window_starts
        != by_rx[ReceiverChainId("rx_1")].exact_window_starts
    )
    rx0 = by_rx[ReceiverChainId("rx_0")]
    best = max(rx0.points, key=lambda point: point.qin_minus_max_surrogate)
    assert best.qin_minus_max_surrogate > 0.5
    assert best.finite_upper_tail_rank == 1
    assert len(best.surrogates) == 4


def test_full_dwell_codec_is_canonical_and_replay_is_deterministic(
    full_dwell_result,
) -> None:
    _, _, result = full_dwell_result
    payload = encode_starlink_full_dwell_response(result)
    decoded = decode_starlink_full_dwell_response(payload)
    assert decoded == result
    assert encode_starlink_full_dwell_response(decoded) == payload
    with pytest.raises(MalformedStarlinkFullDwellResponseError):
        decode_starlink_full_dwell_response(payload + b"\n")


def test_full_dwell_plan_bounds_and_never_relabels_sparse_sampling_as_coverage() -> (
    None
):
    with pytest.raises(ValueError, match="gaps"):
        StarlinkFullDwellPlanV0_1(100, 101, 20, 10, 2, 4)
    with pytest.raises(ValueError, match="room"):
        StarlinkFullDwellPlanV0_1(100, 100, 20, 10, MAXIMUM_EXACT_WINDOWS_PER_STREAM, 4)
