from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from leo_flow.analysis.recording.starlink_full_dwell_timeline_codec import (
    MalformedFullDwellTimelineError,
    decode_full_dwell_timeline,
    encode_full_dwell_timeline,
)
from leo_flow.analysis.recording.starlink_full_dwell_timeline_product import (
    CompleteIqTimelineAnalyzerV0_1,
    contiguous_tile_intervals_v0_1,
    refinement_request_v0_1,
)
from leo_flow.contracts.core import Digest, ReceiverChainId, SchemaRef
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_full_dwell_timeline_product import (
    V0_1,
    FullDwellTimelineBundleV0_1,
    FullDwellTimelinePlanV0_1,
    FullDwellTimelineRequestV0_1,
    FullDwellTimelineStreamSelectionV0_1,
)
from leo_flow.storage.ports import RecordingView

from .fakes import FakeRecordingView, SegmentFixture, execution_context, make_view


def _ci16(sample_count: int) -> bytes:
    raw = bytearray()
    for index in range(sample_count):
        for receiver in range(2):
            value = 10 + receiver * 20 + index % 7
            raw.extend(value.to_bytes(2, "little", signed=True))
            raw.extend((-value).to_bytes(2, "little", signed=True))
    return bytes(raw)


def _case(sample_count: int, *, sample_rate_hz: int = 1_000, tile_samples: int = 8):
    original, recording_ref = make_view(
        SegmentFixture(_ci16(sample_count), sample_rate_hz)
    )
    segment = original.manifest.segments[0]
    tagged = replace(
        segment,
        requested=replace(
            segment.requested, tags=(("channel", "4"), ("edge", "lower"))
        ),
    )
    view = FakeRecordingView(
        replace(original.manifest, segments=(tagged,)),
        {segment.segment_id: _ci16(sample_count)},
    )
    plan = FullDwellTimelinePlanV0_1(tile_samples, 16_384, 4)
    selections = tuple(
        FullDwellTimelineStreamSelectionV0_1(
            view.manifest.radio_id,
            lnb,
            segment.segment_id,
            receiver,
            4,
            StarlinkEdge.LOWER,
            float(sample_rate_hz),
            sample_count,
        )
        for lnb, receiver in (
            ("lnb-a", ReceiverChainId("rx_0")),
            ("lnb-c", ReceiverChainId("rx_1")),
        )
    )
    request = FullDwellTimelineRequestV0_1(
        SchemaRef(FullDwellTimelineRequestV0_1.SCHEMA_ID, V0_1),
        recording_ref.recording_id,
        recording_ref,
        plan,
        selections,
        SchemaRef(FullDwellTimelineBundleV0_1.SCHEMA_ID, V0_1),
    )
    bundle = CompleteIqTimelineAnalyzerV0_1(execution_context()).analyze(
        cast(RecordingView, view), request
    )
    return view, request, bundle


def test_real_20_and_60_second_geometry_stays_within_the_frozen_bound() -> None:
    tile_samples = round(2_500_000 * 0.008)
    maximum_windows = 16_384
    assert len(contiguous_tile_intervals_v0_1(2_500_000 * 20, tile_samples)) == 2_500
    windows_60s = len(contiguous_tile_intervals_v0_1(2_500_000 * 60, tile_samples))
    assert windows_60s == 7_500
    assert windows_60s <= maximum_windows


@pytest.mark.parametrize(("duration_s", "expected_windows"), ((20, 2_500), (60, 7_500)))
def test_20_and_60_second_products_account_for_every_sample(
    duration_s: int, expected_windows: int
) -> None:
    # Scale the sample rate while retaining the actual 8 ms/window counts.
    view, _request, bundle = _case(duration_s * 1_000)
    assert all(len(stream.windows) == expected_windows for stream in bundle.streams)
    for stream in bundle.streams:
        assert stream.covered_sample_count == duration_s * 1_000
        assert stream.coverage_fraction == 1.0
        assert stream.windows[0].start_sample == 0
        assert stream.windows[-1].stop_sample == duration_s * 1_000
        assert all(
            left.stop_sample == right.start_sample
            for left, right in zip(stream.windows, stream.windows[1:])
        )
    assert max(stop - start for _segment, start, stop in view.calls) == 8
    assert len(view.calls) == expected_windows  # each interleaved tile is read once


def test_interleaved_receiver_powers_are_exact_and_share_each_tile_read() -> None:
    view, _request, bundle = _case(19)

    expected_rx0 = sum(2 * (10 + index % 7) ** 2 for index in range(8)) / 8
    expected_rx1 = sum(2 * (30 + index % 7) ** 2 for index in range(8)) / 8
    assert bundle.streams[0].windows[0].mean_complex_power == expected_rx0
    assert bundle.streams[1].windows[0].mean_complex_power == expected_rx1
    assert view.calls == [
        (bundle.streams[0].segment_id, 0, 8),
        (bundle.streams[0].segment_id, 8, 16),
        (bundle.streams[0].segment_id, 16, 19),
    ]


def test_short_tail_is_persisted_once_without_gap_or_overlap() -> None:
    _view, _request, bundle = _case(19)
    assert tuple(
        (window.start_sample, window.stop_sample)
        for window in bundle.streams[0].windows
    ) == ((0, 8), (8, 16), (16, 19))


def test_streams_keep_radio_lnb_receiver_segment_and_edge_identity_unpooled() -> None:
    _view, _request, bundle = _case(19)
    assert tuple(
        (
            stream.radio_id,
            stream.lnb_id,
            stream.receiver_chain_id,
            stream.segment_id,
            stream.edge,
        )
        for stream in bundle.streams
    ) == tuple(
        (
            selection.radio_id,
            selection.lnb_id,
            selection.receiver_chain_id,
            selection.segment_id,
            selection.edge,
        )
        for selection in _request.stream_selections
    )


def test_replay_codec_and_refinement_policy_are_deterministic() -> None:
    _view, request, first = _case(19)
    _view2, request2, second = _case(19)
    assert request2 == request
    assert second == first
    payload = encode_full_dwell_timeline(first)
    assert decode_full_dwell_timeline(payload) == first
    assert encode_full_dwell_timeline(decode_full_dwell_timeline(payload)) == payload
    refinement = refinement_request_v0_1(request, first, Digest.sha256(payload))
    assert len(refinement.windows) == 6
    assert refinement.candidate_only
    assert refinement.selection_policy == request.plan.refinement_selection
    with pytest.raises(MalformedFullDwellTimelineError):
        decode_full_dwell_timeline(payload + b"\n")


def test_declared_resource_bounds_reject_unbounded_60_second_geometry() -> None:
    with pytest.raises(ValueError, match="geometry"):
        _case(60_000, tile_samples=1)
