from __future__ import annotations

import pytest

from leo_flow.contracts.capture_batch import (
    CaptureBatchMode,
    PairedAnalysisEligibility,
)
from leo_flow.contracts.core import (
    CaptureAttemptId,
    CaptureBatchId,
    PlanId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.dashboard_batch import (
    CoordinationClaim,
    DashboardAnalysisState,
    DashboardCaptureState,
)
from leo_flow.contracts.dashboard_master_capture import (
    MAX_MASTER_CAPTURE_RECORDINGS,
    MasterCaptureAttemptV0_1,
    MasterCaptureBatchV0_1,
    MasterCaptureDopplerCandidateV0_1,
    MasterCaptureDopplerV0_1,
    MasterCaptureObservationV0_1,
    MasterCapturePilotV0_1,
    MasterCaptureQamCandidateV0_1,
    MasterCaptureQamV0_1,
    MasterCaptureRetroQamCanaryV0_1,
    MasterCaptureSatelliteV0_1,
    MasterCaptureSnapshotQueryV0_1,
    MasterCaptureSnapshotV0_1,
    MasterCaptureSummaryState,
)
from leo_flow.contracts.dashboard_observation import ObservationAggregateViewV0_1
from leo_flow.contracts.starlink import StarlinkEdge


def test_snapshot_contract_preserves_batch_and_unpooled_receiver_identity() -> None:
    attempt = _attempt()
    batch = MasterCaptureBatchV0_1(
        CaptureBatchId("cbatch_1"),
        CaptureBatchMode.COORDINATED,
        CoordinationClaim.MEASURED_SOFTWARE_COORDINATION,
        (attempt, _attempt(second=True)),
        2,
        0,
        1,
        10,
        PairedAnalysisEligibility.ELIGIBLE,
    )
    view = MasterCaptureSnapshotV0_1(
        1,
        UtcNs(1),
        UtcNs(100),
        (batch,),
        None,
        MasterCaptureObservationV0_1(
            MasterCaptureSummaryState.COMPLETE,
            ObservationAggregateViewV0_1(
                1,
                UtcNs(1),
                UtcNs(100),
                0,
                0,
                0,
                0,
                "required",
                "whole-search-calibration-required",
                (),
                (),
                (),
                False,
            ),
            (),
        ),
        MasterCaptureRetroQamCanaryV0_1(
            MasterCaptureSummaryState.UNAVAILABLE,
            None,
            ("retro-qam-canary-unavailable",),
        ),
        (
            "candidate-only-qam-goodness-not-starlink-detection",
            "radio-lnb-receiver-series-are-never-pooled",
        ),
    )

    assert view.items[0].attempts[0].detail_href == "/recordings/rec_1"
    assert view.items[0].attempts[0].qam.state is MasterCaptureSummaryState.COMPLETE
    assert len(view.items[0].attempts[0].qam.candidates) == 2


def test_missing_qam_is_a_terminal_state_and_never_a_numeric_zero() -> None:
    for state in (
        MasterCaptureSummaryState.PENDING,
        MasterCaptureSummaryState.NO_CANDIDATE,
        MasterCaptureSummaryState.NOT_ANALYZED,
        MasterCaptureSummaryState.FAILED,
        MasterCaptureSummaryState.UNAVAILABLE,
    ):
        summary = MasterCaptureQamV0_1(state, (), (f"{state.value}-reason",))
        assert summary.candidates == ()
    with pytest.raises(ValueError, match="complete QAM"):
        MasterCaptureQamV0_1(MasterCaptureSummaryState.COMPLETE, (), ())


def test_master_capture_query_is_bounded() -> None:
    assert MAX_MASTER_CAPTURE_RECORDINGS == 100
    with pytest.raises(ValueError, match="bound"):
        MasterCaptureSnapshotQueryV0_1(UtcNs(1), UtcNs(2), 101)


def _attempt(*, second: bool = False) -> MasterCaptureAttemptV0_1:
    suffix = "2" if second else "1"
    recording_id = RecordingId(f"rec_{suffix}")
    qam_candidates = tuple(
        MasterCaptureQamCandidateV0_1(
            recording_id,
            RadioId(f"radio_{suffix}"),
            f"lnb_{index}",
            ReceiverChainId(f"rx_{index}"),
            SegmentId(f"seg_{index}"),
            StarlinkEdge.LOWER,
            0.8 - index / 10,
            0.9 - index / 10,
            0.6 + index / 10,
            24,
            f"analysis_{index}",
        )
        for index in range(2)
    )
    doppler = MasterCaptureDopplerV0_1(
        MasterCaptureSummaryState.COMPLETE,
        (
            MasterCaptureDopplerCandidateV0_1(
                recording_id,
                RadioId(f"radio_{suffix}"),
                "lnb_0",
                ReceiverChainId("rx_0"),
                SegmentId("seg_0"),
                "candidate_0",
                "linear",
                123.0,
                0.9,
                "doppler_0",
                "algorithm_0",
            ),
        ),
        (),
    )
    return MasterCaptureAttemptV0_1(
        CaptureAttemptId(f"cattempt_{suffix}"),
        RadioId(f"radio_{suffix}"),
        PlanId(f"plan_{suffix}"),
        UtcNs(10),
        DashboardCaptureState.SUCCEEDED,
        UtcNs(11 if second else 10),
        recording_id,
        None,
        DashboardAnalysisState.COMPLETE,
        True,
        f"/recordings/{recording_id}",
        60_000_000_000,
        MasterCaptureQamV0_1(MasterCaptureSummaryState.COMPLETE, qam_candidates, ()),
        doppler,
        MasterCapturePilotV0_1(
            MasterCaptureSummaryState.UNAVAILABLE,
            None,
            None,
            ("calibrated-pilot-count-unavailable",),
        ),
        MasterCaptureSatelliteV0_1(
            MasterCaptureSummaryState.UNAVAILABLE,
            None,
            ("recording-satellite-association-unavailable",),
        ),
    )
