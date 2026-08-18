from __future__ import annotations

from types import SimpleNamespace

from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.dashboard_capture_doppler import (
    CaptureDopplerHardwareAssignmentV0_1,
    CaptureDopplerScopeRecordingV0_1,
    CaptureDopplerScopeViewV0_1,
)
from leo_flow.contracts.dashboard_capture_qam import (
    CaptureQamState,
    CaptureQamSummaryQueryV0_1,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.services.capture_qam_summary import CaptureQamSummaryQueryServiceV0_1


class _Scope:
    def capture_doppler_scope(self, query):
        del query
        return CaptureDopplerScopeViewV0_1(
            (
                CaptureDopplerScopeRecordingV0_1(
                    RecordingId("rec_qam"),
                    RadioId("radio_a"),
                    "complete",
                    (
                        CaptureDopplerHardwareAssignmentV0_1(
                            ReceiverChainId("rx_a"), "lnb-a"
                        ),
                        CaptureDopplerHardwareAssignmentV0_1(
                            ReceiverChainId("rx_b"), "lnb-b"
                        ),
                    ),
                ),
            ),
            1,
            False,
        )


class _Qam:
    def recording_starlink_acquired_constellation(self, query):
        def stream(receiver: str, lnb: str, accuracy: float, evm: float):
            return SimpleNamespace(
                radio_id=RadioId("radio_a"),
                lnb_id=lnb,
                receiver_chain_id=ReceiverChainId(receiver),
                segment_id=SegmentId(f"seg_{receiver}"),
                edge=StarlinkEdge.LOWER,
                overall=SimpleNamespace(
                    support_weighted_hard_symbol_accuracy=accuracy,
                    support_weighted_rms_evm=evm,
                    window_count=32,
                ),
            )

        return SimpleNamespace(
            streams=(
                stream("rx_a", "lnb-a", 0.80, 0.75),
                stream("rx_b", "lnb-b", 0.26, 4.0),
            ),
            analysis_ref=ArtifactRef(
                "analysis_qam",
                Digest.sha256(b"qam"),
                SchemaRef("org.leo-flow.test-qam"),
            ),
        )


def test_selects_unpooled_qam_goodness_and_ranks_separated_above_noise() -> None:
    result = CaptureQamSummaryQueryServiceV0_1(_Scope(), _Qam()).capture_qam_summaries(
        CaptureQamSummaryQueryV0_1(UtcNs(1), UtcNs(2), 10)
    )
    recording = result.recordings[0]
    assert recording.state is CaptureQamState.COMPLETE
    assert tuple(item.lnb_id for item in recording.candidates) == ("lnb-a", "lnb-b")
    assert recording.candidates[0].qam_goodness > 0.75
    assert recording.candidates[1].qam_goodness < 0.1
    assert result.calibrated_detection_count is None
    assert result.calibration_required
