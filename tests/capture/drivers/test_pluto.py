from __future__ import annotations

import struct
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.capture.drivers.pluto import PlutoPairedRadio, PlutoRadioConfig
from leo_flow.capture.errors import (
    RadioConfigurationError,
    RadioDisconnectedError,
    ReceiverSkewError,
    RefillError,
    SampleCountError,
)
from leo_flow.contracts.capture import GainMode, GainSetting, SegmentRequest
from leo_flow.contracts.continuity import ContinuityPolicy, RefillMetadata
from leo_flow.contracts.core import RadioId, ReceiverChainId, SegmentId
from testkit import FakeClock

RADIO_ID = RadioId("radio_pluto-test")
RECEIVERS = (ReceiverChainId("rx_a"), ReceiverChainId("rx_b"))


class FakePluto:
    def __init__(self, refills: list[object], *, serial: str = "serial-test") -> None:
        self.serial = serial
        self.refills = list(refills)
        self.rx_calls = 0
        self.destroy_calls = 0
        self.close_calls = 0
        self.rx_enabled_channels: Sequence[int] = ()
        self.sample_rate = 0
        self.rx_rf_bandwidth = 0
        self.rx_lo = 0
        self.rx_buffer_size = 0
        self.rx_output_type = "scaled"
        self.gain_control_mode_chan0 = "unset"
        self.gain_control_mode_chan1 = "unset"
        self.rx_hardwaregain_chan0 = 0.0
        self.rx_hardwaregain_chan1 = 0.0
        self._ctx = FakeContext()

    def rx_destroy_buffer(self) -> None:
        self.destroy_calls += 1

    def close(self) -> None:
        self.close_calls += 1
        self._ctx = None

    def _rx_buffered_data(self):
        self.rx_calls += 1
        if not self.refills:
            raise RuntimeError("no scripted refill")
        result = self.refills.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeContext:
    def __init__(self) -> None:
        self.timeout_calls: list[int] = []

    def set_timeout(self, timeout_ms: int) -> None:
        self.timeout_calls.append(timeout_ms)


class DeviceFactory:
    def __init__(self, device: FakePluto) -> None:
        self.device = device
        self.uris: list[str] = []

    def __call__(self, uri: str) -> FakePluto:
        self.uris.append(uri)
        return self.device


def config(**changes) -> PlutoRadioConfig:
    base = PlutoRadioConfig(
        "ip:192.0.2.1",
        "serial-test",
        RADIO_ID,
        RECEIVERS,
        block_samples=3,
    )
    return replace(base, **changes)


def request(samples: int = 5, gain: GainSetting | None = None) -> SegmentRequest:
    return SegmentRequest(
        SegmentId("seg_test"),
        1_500_000_000.0,
        2_500_000.0,
        2_000_000.0,
        RECEIVERS,
        gain or GainSetting(GainMode.MANUAL, 42.5),
        sample_count=samples,
    )


def refill(start: int, samples: int):
    return (
        [index * 10 for index in range(start, start + samples)],
        [index * 10 + 1 for index in range(start, start + samples)],
        [index * 10 + 2 for index in range(start, start + samples)],
        [index * 10 + 3 for index in range(start, start + samples)],
    )


def pure_interleaver(value: object, expected_channels: int) -> bytes:
    assert isinstance(value, tuple) and len(value) == expected_channels * 2
    i0, q0, i1, q1 = value
    if len({len(i0), len(q0), len(i1), len(q1)}) != 1:
        raise ReceiverSkewError("component lengths differ")
    values: list[int] = []
    for components in zip(i0, q0, i1, q1, strict=True):
        values.extend(components)
    return struct.pack(f"<{len(values)}h", *values)


def expected_bytes(samples: int) -> bytes:
    values = [
        value
        for index in range(samples)
        for value in (index * 10, index * 10 + 1, index * 10 + 2, index * 10 + 3)
    ]
    return struct.pack(f"<{len(values)}h", *values)


def metadata(index: int, offset: int, sequence: int) -> RefillMetadata:
    return RefillMetadata(
        index,
        offset,
        3,
        5,
        10 + index,
        sequence,
        1_000 + index * 100,
        1_050 + index * 100,
        1_700_000_000_000_001_000 + index * 100,
        1_700_000_000_000_001_050 + index * 100,
        10,
        (40.0, 41.0),
        (40.0, 41.0),
        (50.0, 51.0),
        (50.0, 51.0),
    )


def adapter(device: FakePluto, **changes) -> tuple[PlutoPairedRadio, DeviceFactory]:
    factory = DeviceFactory(device)
    return (
        PlutoPairedRadio(
            config(**changes),
            device_factory=factory,
            interleaver=pure_interleaver,
            clock=FakeClock(),
        ),
        factory,
    )


def test_one_context_and_one_paired_refill_preserve_exact_native_order() -> None:
    device = FakePluto([refill(0, 3), refill(3, 3), refill(0, 3), refill(3, 3)])
    radio, factory = adapter(device)
    first: list[bytes] = []
    second: list[bytes] = []
    first_manifest = radio.acquire_segment(request(), first.append)
    second_manifest = radio.acquire_segment(request(), second.append)
    assert factory.uris == ["ip:192.0.2.1"]
    assert device.rx_calls == 4
    assert device.destroy_calls == 2
    assert b"".join(first) == expected_bytes(5)
    assert b"".join(second) == expected_bytes(5)
    assert first_manifest.shape == (5, 2, 2)
    assert first_manifest.sample_count == second_manifest.sample_count == 5
    assert dict(first_manifest.diagnostics)["discarded_tail_samples"] == 1
    assert device.rx_enabled_channels == [0, 1]
    assert device.rx_buffer_size == 3
    assert device.rx_output_type == "raw"
    assert device._ctx.timeout_calls == [5_000]


def test_io_timeout_and_shutdown_are_bounded_owned_and_idempotent() -> None:
    class MetadataReader:
        def __init__(self) -> None:
            self.close_calls = 0

        def __call__(self, *_args):  # type: ignore[no-untyped-def]
            raise AssertionError("metadata capture is not needed")

        def close(self) -> None:
            self.close_calls += 1

    device = FakePluto([refill(0, 3)])
    metadata_reader = MetadataReader()
    radio = PlutoPairedRadio(
        config(io_timeout_ms=731),
        device_factory=DeviceFactory(device),
        interleaver=pure_interleaver,
        metadata_reader=metadata_reader,
    )
    radio.acquire_segment(request(2), lambda _data: None)
    assert device._ctx.timeout_calls == [731]
    assert metadata_reader.close_calls == 1
    assert device.destroy_calls == 1

    radio.close()
    radio.close()
    assert metadata_reader.close_calls == 2
    assert device.destroy_calls == 2
    assert device.close_calls == 1
    with pytest.raises(RadioDisconnectedError, match="closed Pluto"):
        radio.acquire_segment(request(2), lambda _data: None)


def test_missing_timeout_capability_fails_closed_and_releases_device() -> None:
    device = FakePluto([refill(0, 3)])
    del device._ctx
    radio = PlutoPairedRadio(
        config(),
        device_factory=DeviceFactory(device),
        interleaver=pure_interleaver,
    )
    with pytest.raises(RadioConfigurationError, match="context timeout"):
        radio.acquire_segment(request(2), lambda _data: None)
    assert device.destroy_calls == 1
    assert device.close_calls == 1


def test_v5_path_keeps_iq_and_normalized_metadata_associated() -> None:
    device = FakePluto([])
    scripted = iter(
        ((refill(0, 3), metadata(0, 0, 100)), (refill(3, 3), metadata(1, 3, 103)))
    )
    radio = PlutoPairedRadio(
        config(host_libiio_version="0.25+c26258b"),
        device_factory=DeviceFactory(device),
        interleaver=pure_interleaver,
        metadata_reader=lambda _device, _index, _offset: next(scripted),
        clock=FakeClock(),
    )
    captured: list[tuple[bytes, RefillMetadata | None]] = []
    manifest = radio.acquire_segment_with_metadata(
        request(6), lambda iq, md: captured.append((iq, md))
    )
    assert b"".join(item[0] for item in captured) == expected_bytes(6)
    assert [item[1].first_sample_sequence for item in captured if item[1]] == [100, 103]
    assert manifest.sample_count == 6
    assert radio.capture_provenance.firmware_release.endswith("metadata-v5")


def test_v5_fails_closed_without_metadata_but_opt_in_fallback_is_labeled() -> None:
    strict, _ = adapter(FakePluto([refill(0, 3)]))
    with pytest.raises(RadioConfigurationError, match="metadata reader"):
        strict.acquire_segment_with_metadata(request(3), lambda _iq, _md: None)

    fallback, _ = adapter(
        FakePluto([refill(0, 3)]),
        continuity_policy=ContinuityPolicy.ALLOW_UNVERIFIED,
    )
    captured: list[tuple[bytes, RefillMetadata | None]] = []
    fallback.acquire_segment_with_metadata(
        request(3), lambda iq, md: captured.append((iq, md))
    )
    assert captured[0][1] is None
    assert (
        fallback.capture_provenance.capability
        == "ordinary-buffer;continuity-unverified"
    )


def test_v5_verified_capture_rejects_partial_refill_tail() -> None:
    radio, _ = adapter(FakePluto([]), host_libiio_version="0.25+c26258b")
    radio._metadata_reader = lambda _device, _index, _offset: (  # type: ignore[attr-defined]
        refill(0, 3),
        metadata(0, 0, 100),
    )
    with pytest.raises(RadioConfigurationError, match="align"):
        radio.acquire_segment_with_metadata(request(5), lambda _iq, _md: None)


def test_manual_gain_and_tuning_are_set_and_read_back() -> None:
    device = FakePluto([refill(0, 3)])
    radio, _ = adapter(device)
    manifest = radio.acquire_segment(request(2), lambda _: None)
    assert device.rx_lo == 1_500_000_000
    assert device.sample_rate == 2_500_000
    assert device.rx_rf_bandwidth == 2_000_000
    assert device.gain_control_mode_chan0 == device.gain_control_mode_chan1 == "manual"
    assert device.rx_hardwaregain_chan0 == device.rx_hardwaregain_chan1 == 42.5
    assert manifest.actual_gain == GainSetting(GainMode.MANUAL, 42.5)


def test_agc_sets_both_channels_without_writing_manual_gain() -> None:
    device = FakePluto([refill(0, 3)])
    radio, _ = adapter(device, agc_mode="fast_attack")
    manifest = radio.acquire_segment(
        request(2, GainSetting(GainMode.AGC)), lambda _: None
    )
    assert device.gain_control_mode_chan0 == "fast_attack"
    assert device.gain_control_mode_chan1 == "fast_attack"
    assert device.rx_hardwaregain_chan0 == device.rx_hardwaregain_chan1 == 0.0
    assert manifest.actual_gain == GainSetting(GainMode.AGC)


def test_readback_and_serial_mismatches_fail_closed() -> None:
    class WrongReadback(FakePluto):
        def __setattr__(self, name, value):
            if name == "sample_rate" and value:
                value += 100
            super().__setattr__(name, value)

    radio, _ = adapter(WrongReadback([refill(0, 3)]))
    with pytest.raises(RadioConfigurationError, match="sample rate"):
        radio.acquire_segment(request(2), lambda _: None)
    wrong_device = FakePluto([refill(0, 3)], serial="other")
    wrong_serial, _ = adapter(wrong_device)
    with pytest.raises(RadioConfigurationError, match="serial mismatch"):
        wrong_serial.acquire_segment(request(2), lambda _: None)
    assert wrong_device.destroy_calls == 1
    assert wrong_device.close_calls == 1


@pytest.mark.parametrize(
    ("refills", "error"),
    [
        ([(refill(0, 2)[0], *refill(0, 1)[1:])], ReceiverSkewError),
        ([refill(0, 1)], SampleCountError),
        ([OSError("USB removed")], RadioDisconnectedError),
        ([RuntimeError("driver refill error")], RefillError),
    ],
)
def test_skew_short_error_and_disconnect_are_distinct(refills, error) -> None:
    radio, _ = adapter(FakePluto(refills))
    with pytest.raises(error):
        radio.acquire_segment(request(2), lambda _: None)


def test_health_counter_increase_rejects_otherwise_complete_capture() -> None:
    readings = iter(({"rx_overflow": 0}, {"rx_overflow": 1}))
    device = FakePluto([refill(0, 3)])
    radio = PlutoPairedRadio(
        config(),
        device_factory=DeviceFactory(device),
        interleaver=pure_interleaver,
        health_reader=lambda _: next(readings),
    )
    with pytest.raises(RefillError, match="health counter"):
        radio.acquire_segment(request(2), lambda _: None)


def test_default_numpy_interleaver_round_trips_full_ci16_range() -> None:
    np = pytest.importorskip("numpy")
    components = (
        np.array([-32768, 1, 10], dtype="<i2"),
        np.array([32767, -2, 11], dtype="<i2"),
        np.array([300, -5, 12], dtype="<i2"),
        np.array([-400, 6, 13], dtype="<i2"),
    )
    device = FakePluto([components])
    radio = PlutoPairedRadio(config(), device_factory=DeviceFactory(device))
    captured: list[bytes] = []
    radio.acquire_segment(request(2), captured.append)
    assert b"".join(captured) == struct.pack(
        "<8h", -32768, 32767, 300, -400, 1, -2, -5, 6
    )


def test_default_native_interleaver_rejects_non_ci16_components() -> None:
    np = pytest.importorskip("numpy")
    components = tuple(np.array([1, 2, 3], dtype="<i4") for _ in range(4))
    radio = PlutoPairedRadio(
        config(), device_factory=DeviceFactory(FakePluto([components]))
    )
    with pytest.raises(SampleCountError, match="signed int16"):
        radio.acquire_segment(request(2), lambda _: None)


def test_import_and_construction_do_not_load_hardware_extra() -> None:
    root = Path(__file__).resolve().parents[3]
    script = """
import sys
from leo_flow.capture.drivers import PlutoPairedRadio, PlutoRadioConfig
from leo_flow.contracts.core import RadioId, ReceiverChainId
assert 'adi' not in sys.modules
config = PlutoRadioConfig('ip:test', 'serial', RadioId('radio_test'), (ReceiverChainId('rx_a'), ReceiverChainId('rx_b')))
PlutoPairedRadio(config)
assert 'adi' not in sys.modules
assert 'numpy' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-S", "-c", script],
        env={"PYTHONPATH": str(root / "src")},
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
