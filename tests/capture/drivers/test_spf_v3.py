from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from leo_flow.capture.drivers.spf_v3 import (
    SpfV3MetadataReader,
    normalize_spf_v3_metadata,
)
from leo_flow.capture.errors import RefillError
from leo_flow.contracts.continuity import RefillFlag

VALID_FLAGS = (
    (1 << 0)
    | (1 << 1)
    | (1 << 4)
    | (1 << 15)
    | (1 << 16)
    | (1 << 18)
    | (1 << 19)
    | (1 << 21)
)


@dataclass(frozen=True)
class Observation:
    sample_sequence_before: int = 90
    sample_sequence_after: int = 110
    read_duration_ns: int = 100
    flags: int = 3
    rx1_gain_db: int = 10
    rx2_gain_db: int = 11


@dataclass(frozen=True)
class Wire:
    flags: int = VALID_FLAGS
    stream_id: int = 7
    buffer_sequence: int = 3
    first_sample_sequence: int = 100
    samples_per_channel: int = 4
    iq_payload_bytes: int = 32
    enabled_scan_mask: int = 0x0F
    channel_count: int = 2
    rx1_gain_db_start: int = 10
    rx2_gain_db_start: int = 11
    rx1_gain_db_end: int = 12
    rx2_gain_db_end: int = 13
    rx1_rssi_start_qdb: int = 267
    rx2_rssi_start_qdb: int = 268
    rx1_rssi_end_qdb: int = 269
    rx2_rssi_end_qdb: int = 270
    gain_observation_overflow_count: int = 0
    gain_event_overflow_count: int = 0
    gain_observations: tuple[Observation, ...] = (Observation(),)


def timing() -> dict[str, object]:
    return {
        "sample_time_valid": True,
        "sample_time_monotonic_start_ns": 1_000,
        "sample_time_monotonic_end_ns": 2_000,
        "sample_time_realtime_start_ns": 3_000,
        "sample_time_realtime_end_ns": 4_000,
        "sample_time_uncertainty_ns": 50,
    }


def test_normalizes_v5_endpoints_observations_and_failure_flags() -> None:
    wire = replace(
        Wire(),
        flags=VALID_FLAGS | (1 << 11),
        gain_observation_overflow_count=2,
    )
    value = normalize_spf_v3_metadata(
        wire, timing(), refill_index=2, segment_sample_offset=8
    )

    assert value.sample_count == 4
    assert value.buffer_sequence == 3
    assert value.gain_db_start == (10.0, 11.0)
    assert value.rssi_db_start == (66.75, 67.0)
    assert value.gain_observations[0].gain_db == (10.0, 11.0)
    assert value.gain_observation_overflow_count == 2
    assert value.flags == (RefillFlag.DEVICE_IIO_OVERFLOW,)


@pytest.mark.parametrize(
    ("wire", "time", "message"),
    (
        (replace(Wire(), flags=VALID_FLAGS & ~(1 << 4)), timing(), "validity flags"),
        (replace(Wire(), enabled_scan_mask=3), timing(), "paired RX1/RX2"),
        (replace(Wire(), iq_payload_bytes=31), timing(), "byte count"),
        (Wire(), {**timing(), "sample_time_valid": False}, "time is not valid"),
        (
            replace(Wire(), gain_observations=(replace(Observation(), flags=1),)),
            timing(),
            "sample interval",
        ),
    ),
)
def test_rejects_untrusted_wire_facts(
    wire: Wire, time: dict[str, object], message: str
) -> None:
    with pytest.raises(RefillError, match=message):
        normalize_spf_v3_metadata(wire, time, refill_index=0, segment_sample_offset=0)


def test_reader_reopens_each_segment_and_returns_native_component_order() -> None:
    numpy = pytest.importorskip("numpy")

    class Device:
        sample_rate = 2_500_000
        rx_buffer_size = 4

        def _rx_buffered_data(self) -> object:
            raise AssertionError("ordinary path must not be used")

    class Session:
        def __init__(self) -> None:
            self.opened = False
            self.closed = False

        def open(self) -> None:
            self.opened = True

        def capture(self):  # type: ignore[no-untyped-def]
            signal = numpy.array(
                [[1 + 2j, 3 + 4j, -5 - 6j, 7 + 8j], [9 + 10j] * 4],
                dtype="complex64",
            )
            return signal, Wire(), timing()

        def close(self) -> None:
            self.closed = True

    sessions: list[Session] = []

    def factory(device, *, sample_rate_hz, samples_per_channel):  # type: ignore[no-untyped-def]
        assert isinstance(device, Device)
        assert (sample_rate_hz, samples_per_channel) == (2_500_000, 4)
        session = Session()
        sessions.append(session)
        return session

    reader = SpfV3MetadataReader(factory)
    first, metadata = reader(Device(), 0, 0)
    assert [item.tolist() for item in first] == [
        [1, 3, -5, 7],
        [2, 4, -6, 8],
        [9, 9, 9, 9],
        [10, 10, 10, 10],
    ]
    assert metadata.refill_index == 0
    assert sessions[0].opened

    reader(Device(), 0, 0)
    assert sessions[0].closed
    assert sessions[1].opened
    reader.close()
    assert sessions[1].closed


def test_reader_rejects_nonintegral_or_out_of_range_iq() -> None:
    numpy = pytest.importorskip("numpy")

    class Device:
        sample_rate = 2_500_000
        rx_buffer_size = 4

        def _rx_buffered_data(self) -> object:
            raise AssertionError

    class Session:
        def open(self) -> None:
            pass

        def capture(self):  # type: ignore[no-untyped-def]
            return numpy.full((2, 4), 1.5 + 40_000j), Wire(), timing()

        def close(self) -> None:
            pass

    reader = SpfV3MetadataReader(
        lambda _device, **_kwargs: Session()  # type: ignore[arg-type]
    )
    with pytest.raises(RefillError, match="represented exactly"):
        reader(Device(), 0, 0)


def test_reader_close_is_idempotent_even_when_session_close_fails() -> None:
    class Session:
        close_calls = 0

        def open(self) -> None:
            pass

        def capture(self):  # type: ignore[no-untyped-def]
            raise AssertionError

        def close(self) -> None:
            self.close_calls += 1
            raise OSError("simulated close failure")

    reader = SpfV3MetadataReader(lambda _device, **_kwargs: Session())  # type: ignore[arg-type]
    reader._session = Session()  # type: ignore[attr-defined]
    session = reader._session  # type: ignore[attr-defined]
    with pytest.raises(OSError, match="close failure"):
        reader.close()
    reader.close()
    assert session.close_calls == 1
