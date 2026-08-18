from __future__ import annotations

from leo_flow.contracts.capture import ActivityKind, GainMode
from leo_flow.contracts.core import (
    V0_1,
    ActivityId,
    Digest,
    HardwareSnapshotId,
    PlanId,
    RadioId,
    SchemaRef,
    StationId,
    UtcNs,
)
from leo_flow.contracts.dashboard_advanced_doppler import (
    PublishedAdvancedDopplerPathPointV0_1,
    PublishedAdvancedDopplerPathV0_1,
)
from leo_flow.contracts.dashboard_recording import (
    RecordingCaptureDetailViewV0_1,
    RecordingSegmentViewV0_1,
)
from leo_flow.contracts.dashboard_recording_evidence import (
    RecordingEvidenceContextViewV0_1,
    RecordingEvidenceDopplerQueryV0_1,
    RecordingEvidenceReceiverV0_1,
    RecordingEvidenceRecordingV0_1,
    RecordingEvidenceSegmentV0_1,
)
from leo_flow.services.recording_evidence import (
    RecordingEvidenceAdvancedDopplerQueryServiceV0_1,
    RecordingEvidenceDopplerQueryServiceV0_1,
)
from tests.dashboard.test_doppler_visualization_api import (
    DIGEST,
    RECEIVER_ID,
    RECORDING_ID,
    SEGMENT_ID,
    visualization,
)


def _detail() -> RecordingCaptureDetailViewV0_1:
    segment = RecordingSegmentViewV0_1(
        SEGMENT_ID,
        ActivityId("act_doppler"),
        ActivityKind.DWELL,
        (RECEIVER_ID,),
        UtcNs(1_000_000_000),
        UtcNs(3_000_000_000),
        10_755_000_000.0,
        1_000_000.0,
        1_000_000.0,
        GainMode.MANUAL,
        40.0,
        2_000_000,
    )
    return RecordingCaptureDetailViewV0_1(
        SchemaRef(RecordingCaptureDetailViewV0_1.SCHEMA_ID, V0_1),
        RECORDING_ID,
        PlanId("plan_doppler"),
        StationId("station_gauss"),
        RadioId("radio_a"),
        "serial-a",
        HardwareSnapshotId("hw_a"),
        "capture",
        "host-disciplined",
        UtcNs(1_000_000_000),
        UtcNs(3_000_000_000),
        "complete",
        True,
        DIGEST,
        "<i2",
        ("sample", "receiver", "component"),
        (segment,),
    )


class _Ports:
    def recording_capture_detail(self, recording_id):
        assert recording_id == RECORDING_ID
        return _detail()

    def recording_evidence_context(self, recording_id):
        detail = _detail()
        recording = RecordingEvidenceRecordingV0_1(
            detail.recording_id,
            detail.radio_id,
            detail.radio_serial,
            detail.hardware_snapshot_id,
            detail.capture_started_utc_ns,
            detail.capture_finished_utc_ns,
            detail.analysis_state,
            True,
        )
        receiver = RecordingEvidenceReceiverV0_1(
            RECORDING_ID,
            detail.radio_id,
            RECEIVER_ID,
            0,
            "lnb_a",
            None,
            UtcNs(1),
            None,
        )
        return RecordingEvidenceContextViewV0_1(
            SchemaRef(RecordingEvidenceContextViewV0_1.SCHEMA_ID),
            recording_id,
            None,
            (recording,),
            (receiver,),
            (RecordingEvidenceSegmentV0_1(RECORDING_ID, SEGMENT_ID, (RECEIVER_ID,)),),
            True,
            None,
            (RecordingEvidenceContextViewV0_1.CANDIDATE_WARNING,),
            ("capture-batch-context-unavailable",),
        )

    def recording_doppler_visualization(self, recording_id, layer):
        return visualization(layer)

    def recording_advanced_doppler_paths(self, recording_id):
        assert recording_id == RECORDING_ID
        return (
            PublishedAdvancedDopplerPathV0_1(
                RECORDING_ID,
                SEGMENT_ID,
                RECEIVER_ID,
                Digest.sha256(b"advanced-path"),
                "doppler_advanced:advanced",
                "advanced-path-only",
                50.0,
                (
                    PublishedAdvancedDopplerPathPointV0_1(
                        0,
                        0,
                        500_000,
                        UtcNs(1_000_000_000),
                        UtcNs(1_500_000_000),
                        UtcNs(1_250_000_000),
                        10_755_000_100.0,
                    ),
                    PublishedAdvancedDopplerPathPointV0_1(
                        1,
                        500_000,
                        1_000_000,
                        UtcNs(1_500_000_000),
                        UtcNs(2_000_000_000),
                        UtcNs(1_750_000_000),
                        10_755_000_125.0,
                    ),
                ),
            ),
        )


def test_doppler_exposes_published_total_and_server_derived_bounded_window() -> None:
    ports = _Ports()
    result = RecordingEvidenceDopplerQueryServiceV0_1(
        ports, ports, ports
    ).recording_evidence_doppler(  # type: ignore[arg-type]
        RecordingEvidenceDopplerQueryV0_1(RECORDING_ID)
    )
    assert result.state == "complete"
    assert result.series[0].lnb_id == "lnb_a"
    assert result.series[0].total.drift_rate_hz_s == 25_000.0
    assert result.series[0].total.derivation == "published-blind-doppler-candidate-fit"
    window = result.series[0].windows[0]
    assert (window.start_sample, window.stop_sample) == (0, 1_000_000)
    assert window.drift_rate_hz_s == 25_000.0
    assert window.support_count == 2


def test_doppler_never_relabels_an_unmatched_lnb_or_pools_it() -> None:
    ports = _Ports()
    result = RecordingEvidenceDopplerQueryServiceV0_1(
        ports, ports, ports
    ).recording_evidence_doppler(  # type: ignore[arg-type]
        RecordingEvidenceDopplerQueryV0_1(RECORDING_ID, lnb_ids=("lnb_other",))
    )
    assert result.state == "missing"
    assert result.series == ()


def test_advanced_only_path_exposes_published_total_and_exact_window_scope() -> None:
    ports = _Ports()
    result = RecordingEvidenceAdvancedDopplerQueryServiceV0_1(
        ports, ports, ports
    ).recording_evidence_advanced_doppler(  # type: ignore[arg-type]
        RecordingEvidenceDopplerQueryV0_1(RECORDING_ID)
    )

    assert result.state == "complete"
    assert result.candidate_only is True
    assert result.calibrated_detection_count is None
    assert len(result.series) == 1
    series = result.series[0]
    assert series.association_state == "advanced-path-only"
    assert series.lnb_id == "lnb_a"
    assert series.total.drift_rate_hz_s == 50.0
    assert series.total.derivation == "published-advanced-slope-bank-path-rate"
    window = series.windows[0]
    assert (window.start_sample, window.stop_sample) == (0, 1_000_000)
    assert (
        window.interval_start_utc_ns,
        window.interval_stop_utc_ns,
    ) == (UtcNs(1_000_000_000), UtcNs(2_000_000_000))
    assert window.drift_rate_hz_s == 50.0
    assert window.derivation == "adjacent-published-advanced-path-points-linear-slope"


def test_advanced_only_path_respects_authoritative_lnb_filter() -> None:
    ports = _Ports()
    result = RecordingEvidenceAdvancedDopplerQueryServiceV0_1(
        ports, ports, ports
    ).recording_evidence_advanced_doppler(  # type: ignore[arg-type]
        RecordingEvidenceDopplerQueryV0_1(RECORDING_ID, lnb_ids=("lnb_other",))
    )

    assert result.state == "missing"
    assert result.series == ()
