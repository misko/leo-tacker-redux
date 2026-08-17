from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.contracts.capture import ActivityKind, GainMode
from leo_flow.contracts.core import (
    V0_1,
    ActivityId,
    Digest,
    HardwareSnapshotId,
    PlanId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    StationId,
    UtcNs,
)
from leo_flow.contracts.dashboard_recording import (
    RecordingCaptureDetailViewV0_1,
    RecordingSegmentViewV0_1,
)


def segment(suffix: str = "a", *, started: int = 110) -> RecordingSegmentViewV0_1:
    return RecordingSegmentViewV0_1(
        SegmentId(f"seg_{suffix}"),
        ActivityId("act_scan"),
        ActivityKind.SCAN,
        (ReceiverChainId("rx_a"), ReceiverChainId("rx_b")),
        UtcNs(started),
        UtcNs(started + 10),
        10_755_000_000.0,
        5_000_000.0,
        5_000_000.0,
        GainMode.MANUAL,
        42.0,
        50_000,
    )


def detail() -> RecordingCaptureDetailViewV0_1:
    return RecordingCaptureDetailViewV0_1(
        SchemaRef(RecordingCaptureDetailViewV0_1.SCHEMA_ID, V0_1),
        RecordingId("rec_detail"),
        PlanId("plan_detail"),
        StationId("station_gauss"),
        RadioId("radio_pluto"),
        "serial-19f2",
        HardwareSnapshotId("hw_detail"),
        "leo-v5-capture",
        "host-disciplined",
        UtcNs(100),
        UtcNs(200),
        "complete",
        True,
        Digest.sha256(b"manifest"),
        "<i2",
        ("sample", "receiver", "component"),
        (segment(), segment("b", started=130)),
    )


def test_capture_detail_exposes_exact_manifest_and_tuning_facts() -> None:
    view = detail()
    assert view.plan_id == "plan_detail"
    assert view.manifest_digest == Digest.sha256(b"manifest")
    assert [item.center_frequency_hz for item in view.segments] == [
        10_755_000_000.0,
        10_755_000_000.0,
    ]
    assert view.segments[0].receiver_chain_ids == ("rx_a", "rx_b")


def test_capture_detail_rejects_inferred_or_inconsistent_facts() -> None:
    view = detail()
    with pytest.raises(ValueError, match="chronological"):
        replace(view, segments=tuple(reversed(view.segments)))
    with pytest.raises(ValueError, match="within the capture"):
        replace(view, segments=(replace(view.segments[0], started_utc_ns=UtcNs(90)),))
    with pytest.raises(ValueError, match="unsupported"):
        replace(view, schema=SchemaRef("org.leo-flow.other", V0_1))
    with pytest.raises(ValueError, match="bandwidth"):
        replace(view.segments[0], bandwidth_hz=6_000_000.0)
    with pytest.raises(ValueError, match="manual gain"):
        replace(view.segments[0], gain_db=None)
