from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.capture.errors import RadioDisconnectedError, WriterIdentityError
from leo_flow.capture.fake_radio import Disconnect, FakePairedRadio, Refill, ShortRead
from leo_flow.capture.spool import SpoolState
from leo_flow.contracts.capture import ActivityKind, ActivityRequest
from leo_flow.contracts.core import ActivityId, RecordingId, UtcNs
from testkit import FakeClock

from ._helpers import (
    RADIO_ID,
    RECEIVERS,
    FakeRecordingWriter,
    ci16,
    engine,
    plan_with_activities,
    segment,
    spool,
)


def test_scan_and_dwell_use_the_same_ordered_capture_path(tmp_path) -> None:
    scan_a = segment("seg_scan_a", 2)
    scan_b = segment("seg_scan_b", 3)
    dwell = segment("seg_dwell", 4)
    plan = plan_with_activities(
        (
            ActivityRequest(
                ActivityId("act_scan"), ActivityKind.SCAN, (scan_a, scan_b)
            ),
            ActivityRequest(ActivityId("act_dwell"), ActivityKind.DWELL, (dwell,)),
        )
    )
    scripts = {
        scan_a.segment_id: (Refill(ci16(2)),),
        scan_b.segment_id: (ShortRead(ci16(1)), Refill(ci16(2, start=1))),
        dwell.segment_id: (Refill(ci16(4)),),
    }
    clock = FakeClock()
    radio = FakePairedRadio(RADIO_ID, RECEIVERS, scripts, clock=clock)
    writer = FakeRecordingWriter()
    local_spool = spool(tmp_path)

    completed = engine(clock).execute(plan, radio, writer, local_spool)

    assert radio.acquired_segment_ids == [
        scan_a.segment_id,
        scan_b.segment_id,
        dwell.segment_id,
    ]
    assert [activity.kind for activity in completed.manifest.activities] == [
        ActivityKind.SCAN,
        ActivityKind.DWELL,
    ]
    assert [item.sample_count for item in completed.manifest.segments] == [2, 3, 4]
    assert b"".join(writer.session.blocks[scan_b.segment_id]) == ci16(3)
    assert local_spool.get(completed.recording_id).state is SpoolState.COMPLETE


def test_radio_failure_aborts_writer_and_records_failed_state(tmp_path) -> None:
    plan = plan_with_activities()
    request = plan.activities[0].segments[0]
    radio = FakePairedRadio(RADIO_ID, RECEIVERS, {request.segment_id: (Disconnect(),)})
    writer = FakeRecordingWriter()
    local_spool = spool(tmp_path)
    with pytest.raises(RadioDisconnectedError):
        engine(FakeClock()).execute(plan, radio, writer, local_spool)
    entry = local_spool.get(writer.recording_id)
    assert entry.state is SpoolState.FAILED
    assert "RadioDisconnectedError" in entry.last_error
    assert writer.session.aborted_reason is not None


def test_writer_and_spool_identity_mismatch_fails_closed(tmp_path) -> None:
    plan = plan_with_activities()
    request = plan.activities[0].segments[0]
    radio = FakePairedRadio(
        RADIO_ID, RECEIVERS, {request.segment_id: (Refill(ci16(4)),)}
    )
    writer = FakeRecordingWriter(recording_id_override=RecordingId("rec_different"))
    local_spool = spool(tmp_path)
    with pytest.raises(WriterIdentityError):
        engine(FakeClock()).execute(plan, radio, writer, local_spool)
    assert (
        local_spool.get(RecordingId("rec_01J00000000000000000000000")).state
        is SpoolState.FAILED
    )


def test_radio_may_not_change_the_segment_request(tmp_path) -> None:
    plan = plan_with_activities()
    request = plan.activities[0].segments[0]

    class DishonestRadio:
        radio_id = RADIO_ID

        def acquire_segment(self, requested, write_ci16):
            write_ci16(ci16(4))
            honest = FakePairedRadio(
                RADIO_ID, RECEIVERS, {request.segment_id: (Refill(ci16(4)),)}
            ).acquire_segment(request, lambda _: None)
            return replace(honest, requested=replace(request, center_frequency_hz=1.0))

    local_spool = spool(tmp_path)
    with pytest.raises(Exception, match="different requested settings"):
        engine(FakeClock()).execute(
            plan, DishonestRadio(), FakeRecordingWriter(), local_spool
        )


def test_future_segment_schedule_is_honored_before_tuning(tmp_path) -> None:
    clock = FakeClock()
    requested = replace(
        segment("seg_scheduled", 2),
        scheduled_utc_ns=UtcNs(clock.utc_ns + 2_000_000_000),
    )
    plan = plan_with_activities(
        (ActivityRequest(ActivityId("act_scheduled"), ActivityKind.TEST, (requested,)),)
    )
    radio = FakePairedRadio(
        RADIO_ID,
        RECEIVERS,
        {requested.segment_id: (Refill(ci16(2)),)},
        clock=clock,
    )
    completed = engine(clock).execute(
        plan, radio, FakeRecordingWriter(), spool(tmp_path)
    )
    assert completed.manifest.segments[0].start_utc_ns == requested.scheduled_utc_ns
