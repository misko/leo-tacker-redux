from __future__ import annotations

import pytest

from leo_flow.contracts.core import (
    HardwareSnapshotId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.dashboard_recording_evidence import (
    RecordingEvidenceContextViewV0_1,
    RecordingEvidenceReceiverV0_1,
    RecordingEvidenceRecordingV0_1,
    RecordingEvidenceSegmentV0_1,
)


def context() -> RecordingEvidenceContextViewV0_1:
    recording = RecordingEvidenceRecordingV0_1(
        RecordingId("rec_evidence"),
        RadioId("radio_a"),
        "serial-a",
        HardwareSnapshotId("hw_a"),
        UtcNs(100),
        UtcNs(200),
        "complete",
        True,
    )
    receiver = RecordingEvidenceReceiverV0_1(
        recording.recording_id,
        recording.radio_id,
        ReceiverChainId("rx_a"),
        0,
        "lnb_a",
        "horizontal",
        UtcNs(1),
        None,
    )
    return RecordingEvidenceContextViewV0_1(
        SchemaRef(RecordingEvidenceContextViewV0_1.SCHEMA_ID),
        recording.recording_id,
        None,
        (recording,),
        (receiver,),
        (
            RecordingEvidenceSegmentV0_1(
                recording.recording_id,
                SegmentId("seg_a"),
                (receiver.receiver_chain_id,),
            ),
        ),
        True,
        None,
        (RecordingEvidenceContextViewV0_1.CANDIDATE_WARNING,),
        ("capture-batch-context-unavailable",),
    )


def test_context_keeps_authoritative_hardware_dimensions_and_candidate_semantics() -> (
    None
):
    value = context()
    assert value.receivers[0].lnb_id == "lnb_a"
    assert value.calibrated_detection_count is None
    assert value.limitations == ("capture-batch-context-unavailable",)


def test_context_rejects_inferred_or_missing_requested_scope() -> None:
    value = context()
    with pytest.raises(ValueError, match="one requested recording"):
        RecordingEvidenceContextViewV0_1(
            value.schema,
            value.requested_recording_id,
            value.capture_batch_id,
            tuple(
                RecordingEvidenceRecordingV0_1(
                    item.recording_id,
                    item.radio_id,
                    item.radio_serial,
                    item.hardware_snapshot_id,
                    item.capture_started_utc_ns,
                    item.capture_finished_utc_ns,
                    item.analysis_state,
                    False,
                )
                for item in value.recordings
            ),
            value.receivers,
            value.segments,
            True,
            None,
            value.warnings,
            value.limitations,
        )
