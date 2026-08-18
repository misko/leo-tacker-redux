from __future__ import annotations

from types import SimpleNamespace

from leo_flow.contracts.core import RadioId, ReceiverChainId, RecordingId, SegmentId
from leo_flow.contracts.dashboard_symbolwise_replay import (
    RecordingSymbolwiseReplayDashboardQueryV0_1,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.dashboard.symbolwise_replay import (
    RecordingSymbolwiseReplayDashboardProjectionV0_1,
)


class _Replay:
    def __init__(self) -> None:
        selection = SimpleNamespace(
            radio_id=RadioId("radio_pluto_5d4d"),
            segment_id=SegmentId("seg_lower"),
            receiver_chain_id=ReceiverChainId("rx_lnb_c"),
            edge=StarlinkEdge.LOWER,
            sample_rate_hz=2_500_000.0,
            frequency_center=SimpleNamespace(center_cfo_hz=602_869.4),
            identity=("radio_pluto_5d4d", "seg_lower", "rx_lnb_c", "lower"),
        )
        windows = tuple(
            SimpleNamespace(
                window_index=index,
                start_sample=index * 250_000,
                stop_sample=index * 250_000 + 25_000,
                patterns=tuple(
                    SimpleNamespace(
                        pattern=SimpleNamespace(
                            pattern_id=("qin" if pattern == 0 else f"surrogate-{pattern - 1}"),
                            role=SimpleNamespace(
                                value=(
                                    "qin-exact"
                                    if pattern == 0
                                    else "precommitted-surrogate"
                                )
                            ),
                            codebook_index=None if pattern == 0 else pattern - 1,
                        ),
                        selection_score=0.75 - pattern * 0.05,
                        winning_cfo_hz=600_000.0 + pattern,
                        winning_epoch_sample=pattern,
                    )
                    for pattern in range(5)
                ),
            )
            for index in range(600)
        )
        self.bundle = SimpleNamespace(
            recording_id=RecordingId("rec_symbolwise_product"),
            stream_selections=(selection,),
            streams=(SimpleNamespace(windows=windows),),
            reason_codes=("finite-pattern-controls-not-empirical-null",),
        )
        self.queries = []

    def recording_starlink_symbolwise_replay(self, query):  # type: ignore[no-untyped-def]
        self.queries.append(query)
        streams = tuple(
            SimpleNamespace(
                selection=selection,
                windows=stream.windows[
                    query.first_window_index : query.stop_window_index
                ],
            )
            for selection, stream in zip(
                self.bundle.stream_selections, self.bundle.streams, strict=True
            )
            if not query.receiver_chain_ids
            or selection.receiver_chain_id in query.receiver_chain_ids
        )
        return SimpleNamespace(
            candidates_only=True,
            truncated=False,
            reason_codes=self.bundle.reason_codes,
            streams=streams,
        )


class _Context:
    def recording_evidence_context(self, recording_id):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            receivers=(
                SimpleNamespace(
                    recording_id=recording_id,
                    radio_id=RadioId("radio_pluto_5d4d"),
                    receiver_chain_id=ReceiverChainId("rx_lnb_c"),
                    lnb_id="lnb-c",
                ),
            )
        )


def test_projection_reads_three_bounded_slices_and_derives_complete_unpooled_view() -> None:
    replay = _Replay()
    projection = RecordingSymbolwiseReplayDashboardProjectionV0_1(
        replay, _Context()
    )
    view = projection.recording_symbolwise_replay_dashboard(
        RecordingSymbolwiseReplayDashboardQueryV0_1(
            replay.bundle.recording_id,
            (RadioId("radio_pluto_5d4d"),),
            ("lnb-c",),
            (ReceiverChainId("rx_lnb_c"),),
        )
    )

    assert [(item.first_window_index, item.stop_window_index) for item in replay.queries] == [
        (0, 200),
        (200, 400),
        (400, 600),
    ]
    assert view.stream_count == 1
    assert view.point_count == 600
    stream = view.streams[0]
    assert (stream.radio_id, stream.lnb_id, stream.receiver_chain_id) == (
        RadioId("radio_pluto_5d4d"),
        "lnb-c",
        ReceiverChainId("rx_lnb_c"),
    )
    assert (stream.window_duration_ms, stream.cadence_ms) == (10, 100)
    assert (stream.analyzed_union_fraction, stream.analyzed_union_percent) == (
        0.1,
        10.0,
    )
    assert len(stream.windows) == 600
    assert (stream.windows[0].start_time_s, stream.windows[-1].start_time_s) == (
        0.0,
        59.9,
    )
    assert stream.windows[0].patterns[0].candidate_label == (
        "Candidate-only · Qin exact"
    )
    assert stream.windows[0].patterns[1].candidate_label == (
        "Candidate-only · surrogate 1"
    )
    assert stream.overall[0].winning_window_index == 0
    assert "all-600-fixed-cadence-windows" in stream.overall[0].derivation
    assert view.calibrated_detection_count is None
    assert "no-cross-hardware-pooling" in view.summary_derivation


def test_projection_applies_authoritative_hardware_filters_before_blob_reads() -> None:
    replay = _Replay()
    projection = RecordingSymbolwiseReplayDashboardProjectionV0_1(
        replay, _Context()
    )
    view = projection.recording_symbolwise_replay_dashboard(
        RecordingSymbolwiseReplayDashboardQueryV0_1(
            replay.bundle.recording_id, lnb_ids=("another-lnb",)
        )
    )
    assert view.streams == ()
    assert replay.queries == []
