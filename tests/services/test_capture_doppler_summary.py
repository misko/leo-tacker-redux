from __future__ import annotations

from leo_flow.contracts.core import RadioId, ReceiverChainId, RecordingId, UtcNs
from leo_flow.contracts.dashboard_capture_doppler import (
    CaptureDopplerHardwareAssignmentV0_1,
    CaptureDopplerScopeRecordingV0_1,
    CaptureDopplerScopeViewV0_1,
    CaptureDopplerState,
    CaptureDopplerSummaryQueryV0_1,
)
from leo_flow.contracts.dashboard_doppler_aggregate import (
    DopplerAggregateSeriesV0_1,
    DopplerAggregateTrackPointV0_1,
    DopplerAggregateViewV0_1,
)
from leo_flow.services.capture_doppler_summary import (
    CaptureDopplerSummaryQueryServiceV0_1,
)

WARNINGS = (
    "advanced-path-bins-not-converted-to-physical-frequency",
    "candidate-only-evidence-not-satellite-detection",
    "overlapping-track-observations-are-not-independent",
)


def _series(
    recording: str, radio: str, receiver: str, rank: int, score: float, drift: float
) -> DopplerAggregateSeriesV0_1:
    return DopplerAggregateSeriesV0_1(
        recording,
        UtcNs(100),
        radio,
        receiver,
        f"seg_ch4_lower_{receiver}",
        "CH4",
        "lower",
        f"doppler_{recording}_{receiver}_{rank}",
        f"waterfall_{recording}",
        f"candidate:{recording}:{receiver}:{rank}",
        "basic",
        "0.1.0",
        "linear",
        "basic-candidate",
        UtcNs(100),
        10_755_000_000.0,
        drift,
        score,
        "sha256:" + "1" * 64,
        "sha256:" + "2" * 64,
        "sha256:" + "3" * 64,
        "sha256:" + "4" * 64,
        True,
        (
            DopplerAggregateTrackPointV0_1(UtcNs(100), 0.0, 0.0),
            DopplerAggregateTrackPointV0_1(UtcNs(200), 0.0000001, drift * 0.0000001),
        ),
    )


class _Ports:
    def capture_doppler_scope(self, query):
        return CaptureDopplerScopeViewV0_1(
            (
                CaptureDopplerScopeRecordingV0_1(
                    RecordingId("rec_a"),
                    RadioId("radio_a"),
                    "complete",
                    (
                        CaptureDopplerHardwareAssignmentV0_1(
                            ReceiverChainId("rx_a1"), "lnb_a1"
                        ),
                        CaptureDopplerHardwareAssignmentV0_1(
                            ReceiverChainId("rx_a2"), "lnb_a2"
                        ),
                    ),
                ),
                CaptureDopplerScopeRecordingV0_1(
                    RecordingId("rec_b"),
                    RadioId("radio_b"),
                    "complete",
                    (
                        CaptureDopplerHardwareAssignmentV0_1(
                            ReceiverChainId("rx_b1"), "lnb_b1"
                        ),
                        CaptureDopplerHardwareAssignmentV0_1(
                            ReceiverChainId("rx_b2"), "lnb_b2"
                        ),
                    ),
                ),
                CaptureDopplerScopeRecordingV0_1(
                    RecordingId("rec_pending"),
                    RadioId("radio_a"),
                    "running",
                    (
                        CaptureDopplerHardwareAssignmentV0_1(
                            ReceiverChainId("rx_pending"), "lnb_pending"
                        ),
                    ),
                ),
                CaptureDopplerScopeRecordingV0_1(
                    RecordingId("rec_failed"),
                    RadioId("radio_b"),
                    "failed",
                    (
                        CaptureDopplerHardwareAssignmentV0_1(
                            ReceiverChainId("rx_failed"), "lnb_failed"
                        ),
                    ),
                ),
                CaptureDopplerScopeRecordingV0_1(
                    RecordingId("rec_unavailable"),
                    RadioId("radio_a"),
                    "complete",
                    (
                        CaptureDopplerHardwareAssignmentV0_1(
                            ReceiverChainId("rx_unavailable"), "lnb_unavailable"
                        ),
                    ),
                ),
            ),
            5,
            False,
        )

    def doppler_aggregate(self, query):
        series = (
            _series("rec_a", "radio_a", "rx_a1", 1, 9.0, 24_000.0),
            _series("rec_a", "radio_a", "rx_a1", 2, 2.0, -5_000.0),
            _series("rec_a", "radio_a", "rx_a2", 1, 8.0, 26_000.0),
            _series("rec_b", "radio_b", "rx_b1", 1, 7.0, -12_000.0),
            _series("rec_b", "radio_b", "rx_b2", 1, 6.0, -14_000.0),
        )
        return DopplerAggregateViewV0_1(
            1,
            query.start_utc_ns,
            query.stop_utc_ns,
            5,
            4,
            2,
            False,
            series,
            (),
            (),
            WARNINGS,
        )


def test_selects_best_total_fit_independently_for_every_authoritative_receiver() -> (
    None
):
    ports = _Ports()
    result = CaptureDopplerSummaryQueryServiceV0_1(
        ports, ports
    ).capture_doppler_summaries(  # type: ignore[arg-type]
        CaptureDopplerSummaryQueryV0_1(UtcNs(1), UtcNs(1_000))
    )
    rec_a = result.recordings[0]
    rec_b = result.recordings[1]
    assert [
        (item.lnb_id, str(item.receiver_chain_id), item.drift_rate_hz_s)
        for item in rec_a.candidates
    ] == [
        ("lnb_a1", "rx_a1", 24_000.0),
        ("lnb_a2", "rx_a2", 26_000.0),
    ]
    assert [
        (item.lnb_id, str(item.receiver_chain_id)) for item in rec_b.candidates
    ] == [("lnb_b1", "rx_b1"), ("lnb_b2", "rx_b2")]
    assert result.candidate_only is True
    assert result.calibrated_detection_count is None


def test_pending_failed_and_unavailable_states_are_not_zero_measurements() -> None:
    ports = _Ports()
    result = CaptureDopplerSummaryQueryServiceV0_1(
        ports, ports
    ).capture_doppler_summaries(  # type: ignore[arg-type]
        CaptureDopplerSummaryQueryV0_1(UtcNs(1), UtcNs(1_000))
    )
    assert result.recordings[2].state is CaptureDopplerState.PENDING
    assert result.recordings[3].state is CaptureDopplerState.ERROR
    assert result.recordings[4].state is CaptureDopplerState.UNAVAILABLE
    assert result.recordings[2].candidates == result.recordings[3].candidates == ()
    assert result.recordings[4].candidates == ()
