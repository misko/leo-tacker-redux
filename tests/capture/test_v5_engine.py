from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.capture import FakeV5PairedRadio, V5Refill
from leo_flow.contracts.continuity import (
    CaptureProvenance,
    ContinuityStatus,
    RefillFlag,
    RefillMetadata,
)
from testkit import FakeClock
from tests.capture._helpers import (
    RADIO_ID,
    RECEIVERS,
    FakeRecordingWriter,
    ci16,
    engine,
    plan_with_activities,
    spool,
)


def metadata(index: int, sequence: int, *, flags=()) -> RefillMetadata:
    return RefillMetadata(
        index,
        index * 2,
        2,
        4,
        30 + index,
        sequence,
        1000 + index * 100,
        1050 + index * 100,
        1_700_000_000_000_001_000 + index * 100,
        1_700_000_000_000_001_050 + index * 100,
        10,
        (40.0, 41.0),
        (40.0, 41.0),
        (50.0, 51.0),
        (50.0, 51.0),
        flags=flags,
    )


def radio(second: RefillMetadata) -> FakeV5PairedRadio:
    segment_id = plan_with_activities().activities[0].segments[0].segment_id
    return FakeV5PairedRadio(
        RADIO_ID,
        RECEIVERS,
        {
            segment_id: (
                V5Refill(ci16(2), metadata(0, 100)),
                V5Refill(ci16(2, start=2), second),
            )
        },
        CaptureProvenance("v5", "commit", "0.25", "v3", "metadata=1"),
        clock=FakeClock(),
    )


def test_engine_publishes_verified_refill_facts(tmp_path) -> None:
    writer = FakeRecordingWriter()
    completed = engine(FakeClock()).execute(
        plan_with_activities(), radio(metadata(1, 102)), writer, spool(tmp_path)
    )
    continuity = writer.session.continuities[completed.manifest.segments[0].segment_id]
    assert continuity.status is ContinuityStatus.VERIFIED
    assert [item.buffer_sequence for item in continuity.refills] == [30, 31]


@pytest.mark.parametrize(
    ("second", "match"),
    [
        (metadata(1, 103), "sample sequence gap"),
        (
            replace(metadata(1, 102), flags=(RefillFlag.DEVICE_IIO_OVERFLOW,)),
            "failure flags",
        ),
    ],
)
def test_engine_aborts_gap_or_overflow_instead_of_claiming_contiguous(
    tmp_path, second, match
) -> None:
    writer = FakeRecordingWriter()
    with pytest.raises(ValueError, match=match):
        engine(FakeClock()).execute(
            plan_with_activities(), radio(second), writer, spool(tmp_path)
        )
    assert writer.session.aborted_reason is not None
