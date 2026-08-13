from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.capture.errors import (
    RadioDisconnectedError,
    ReceiverSkewError,
    RefillError,
    SampleCountError,
    TuningError,
)
from leo_flow.capture.fake_radio import (
    Delay,
    Disconnect,
    FakePairedRadio,
    MissingRefill,
    ReceiverSkew,
    Refill,
    ShortRead,
    TuningFailure,
)
from testkit import FakeClock

from ._helpers import RADIO_ID, RECEIVERS, ci16, segment


def test_short_and_missing_refills_preserve_exact_bytes_and_order() -> None:
    request = segment("seg_short", 5)
    clock = FakeClock()
    written: list[bytes] = []
    radio = FakePairedRadio(
        RADIO_ID,
        RECEIVERS,
        {
            request.segment_id: (
                ShortRead(ci16(2)),
                MissingRefill(),
                Delay(0.25),
                Refill(ci16(3, start=2)),
            )
        },
        clock=clock,
        delay=lambda seconds: clock.advance_ns(round(seconds * 1e9)),
    )
    manifest = radio.acquire_segment(request, written.append)
    assert b"".join(written) == ci16(5)
    assert manifest.sample_count == 5
    assert manifest.shape == (5, 2, 2)
    assert dict(manifest.diagnostics) == {
        "delay_seconds": 0.25,
        "missing_refills": 1,
        "short_reads": 1,
    }


def test_duration_is_resolved_to_an_exact_sample_count() -> None:
    request = replace(segment("seg_duration"), sample_count=None, duration_s=4e-6)
    expected_samples = round(request.duration_s * request.sample_rate_hz)
    radio = FakePairedRadio(
        RADIO_ID,
        RECEIVERS,
        {request.segment_id: (Refill(ci16(expected_samples)),)},
    )
    manifest = radio.acquire_segment(request, lambda _: None)
    assert manifest.sample_count == 10


@pytest.mark.parametrize(
    ("events", "error"),
    [
        ((TuningFailure("no lock"),), TuningError),
        ((Disconnect("USB gone"),), RadioDisconnectedError),
        ((ReceiverSkew((4, 3)),), ReceiverSkewError),
        ((MissingRefill(), MissingRefill(), MissingRefill()), RefillError),
        ((Refill(ci16(5)),), SampleCountError),
    ],
)
def test_injected_faults_fail_closed(events, error) -> None:
    request = segment("seg_fault", 4)
    radio = FakePairedRadio(RADIO_ID, RECEIVERS, {request.segment_id: events})
    with pytest.raises(error):
        radio.acquire_segment(request, lambda _: None)
