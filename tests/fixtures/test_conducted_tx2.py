from __future__ import annotations

import hashlib
import struct
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from leo_flow.capture.drivers.v5_preflight import (
    ExpectedV5Radio,
    ExpectedV5Runtime,
    ObservedV5Radio,
    ObservedV5Runtime,
)
from leo_flow.fixtures.conducted_tx2 import (
    ALLOWED_WAVEFORM_RMS_COUNTS,
    CONDUCTED_CONFIRMATION,
    CONDUCTED_TOPOLOGY,
    IO_TIMEOUT_MS,
    MAXIMUM_SAMPLE_COUNT,
    ConductedFixtureAttestation,
    ConductedTx2Plan,
    FiniteTx2Waveform,
    PyadiTx2Device,
    Tx2CleanupError,
    Tx2LadderStep,
    Tx2SafetyError,
    run_conducted_tx2_ladder,
)

SERIAL = "104000b29905000e17000800065934759d"
URI = "ip:radio-under-test.invalid"
EXPECTED_RUNTIME = ExpectedV5Runtime(
    runtime_id="pluto-v5-libiio-0.25-spfmeta3",
    schema="leo-flow.v5-runtime/v1",
    iio_module_path="/qualified/iio.py",
    iio_version=(0, 25, "c26258b"),
    iio_commit="c26258bfa33098c2b215e19cf85d448e89499b1a",
    native_libiio_prefix="/qualified",
    required_backends=frozenset(("local", "ip", "usb")),
    pyadi_version="0.0.21",
    pyadi_module_path="/qualified/adi/__init__.py",
    spf_module_path="/qualified/spf/direct_radio/iio_metadata.py",
    spf_revision="c40ee4116546889effd72056115adaaa1bc3fd40",
    spf_import="spf.direct_radio.iio_metadata:IioMetadataRx",
    metadata_protocol="spf-radio-metadata-v3",
)
EXPECTED_RADIO = ExpectedV5Radio(
    serial=SERIAL,
    firmware_release="v0.38-plutoplus-spf-libiio-metadata-v5",
    firmware_commit="d7c87a9a28094ee6f0b23cb47df9ff737b5a69d8",
)
OBSERVED_RUNTIME = ObservedV5Runtime(
    runtime_id=EXPECTED_RUNTIME.runtime_id,
    schema=EXPECTED_RUNTIME.schema,
    iio_module_path=EXPECTED_RUNTIME.iio_module_path,
    iio_version=EXPECTED_RUNTIME.iio_version,
    iio_commit=EXPECTED_RUNTIME.iio_commit,
    metadata_buffer_present=True,
    native_libiio_paths=("/qualified/lib/libiio.so.0",),
    available_backends=EXPECTED_RUNTIME.required_backends,
    pyadi_version=EXPECTED_RUNTIME.pyadi_version,
    pyadi_module_path=EXPECTED_RUNTIME.pyadi_module_path,
    spf_module_path=EXPECTED_RUNTIME.spf_module_path,
    spf_revision=EXPECTED_RUNTIME.spf_revision,
    spf_import=EXPECTED_RUNTIME.spf_import,
    metadata_protocol=EXPECTED_RUNTIME.metadata_protocol,
)
OBSERVED_RADIO = ObservedV5Radio(
    serial=SERIAL,
    firmware_release=EXPECTED_RADIO.firmware_release,
    metadata_capability=EXPECTED_RADIO.metadata_capability,
    enabled_scan_mask=EXPECTED_RADIO.enabled_scan_mask,
    channel_count=EXPECTED_RADIO.channel_count,
    component_layout=EXPECTED_RADIO.component_layout,
    tx2_hardware_gain_db=-80.0,
    tx2_dds_scales=tuple(
        (channel_id, 0.0) for channel_id in EXPECTED_RADIO.tx2_dds_channel_ids
    ),
)


def waveform(level: int = 16, *, sample_count: int = 64) -> FiniteTx2Waveform:
    data = b"".join(struct.pack("<hh", level, 0) for _ in range(sample_count))
    return FiniteTx2Waveform.from_ci16(data, declared_rms_counts=level)


def topology() -> ConductedFixtureAttestation:
    return ConductedFixtureAttestation(
        radio_serial=SERIAL,
        topology=CONDUCTED_TOPOLOGY,
        splitter_id="tee-bench-01",
        tx2_to_rx1_attenuator_ids=("att-rx1-30db",),
        tx2_to_rx2_attenuator_ids=("att-rx2-30db",),
        tx2_to_rx1_attenuation_db=30.0,
        tx2_to_rx2_attenuation_db=31.0,
        confirmation=CONDUCTED_CONFIRMATION,
    )


def plan(*steps: Tx2LadderStep) -> ConductedTx2Plan:
    return ConductedTx2Plan(
        uri=URI,
        expected_radio_serial=SERIAL,
        armed_radio_serial=SERIAL,
        topology=topology(),
        expected_runtime=EXPECTED_RUNTIME,
        expected_radio=EXPECTED_RADIO,
        tx_lo_hz=1_709_687_500,
        sample_rate_hz=1_250_000,
        steps=steps or (Tx2LadderStep(80, waveform()),),
    )


class FakeTx2:
    def __init__(
        self,
        *,
        serial: str = SERIAL,
        fail_once: str | None = None,
        fail_always: str | None = None,
    ) -> None:
        self.serial = serial
        self.fail_once = fail_once
        self.fail_always = fail_always
        self.failed = False
        self.events: list[str] = []
        self.gain = -10.0
        self.dds = {
            "altvoltage4": 0.5,
            "altvoltage5": 0.5,
            "altvoltage6": 0.5,
            "altvoltage7": 0.5,
        }
        self.lo = 0.0
        self.rate = 0.0
        self.closed = False
        self.transmissions: list[bytes] = []

    def _event(self, name: str) -> None:
        self.events.append(name)
        if self.fail_always == name or (self.fail_once == name and not self.failed):
            self.failed = True
            raise OSError(f"injected {name} failure")

    def read_serial(self) -> str:
        self._event("read_serial")
        return self.serial

    def attest_qualified_v5(
        self,
        uri: str,
        expected_runtime: ExpectedV5Runtime,
        expected_radio: ExpectedV5Radio,
    ) -> None:
        self._event("attest_qualified_v5")
        assert uri == URI
        assert expected_runtime == EXPECTED_RUNTIME
        assert expected_radio == EXPECTED_RADIO

    def destroy_tx_buffer(self) -> None:
        self._event("destroy_tx_buffer")

    def disable_tx2_dds(self) -> None:
        self._event("disable_tx2_dds")
        self.dds = dict.fromkeys(self.dds, 0.0)

    def read_tx2_dds_scales(self) -> dict[str, float]:
        self._event("read_tx2_dds_scales")
        return dict(self.dds)

    def set_tx2_gain_db(self, value: float) -> None:
        self._event("set_tx2_gain_db")
        self.gain = value

    def read_tx2_gain_db(self) -> float:
        self._event("read_tx2_gain_db")
        return self.gain

    def set_tx2_lo_hz(self, value: int) -> None:
        self._event("set_tx2_lo_hz")
        self.lo = float(value)

    def read_tx2_lo_hz(self) -> float:
        self._event("read_tx2_lo_hz")
        return self.lo

    def set_sample_rate_hz(self, value: int) -> None:
        self._event("set_sample_rate_hz")
        self.rate = float(value)

    def read_sample_rate_hz(self) -> float:
        self._event("read_sample_rate_hz")
        return self.rate

    def transmit_tx2_finite_ci16(self, value: bytes) -> None:
        self._event("transmit_tx2_finite_ci16")
        self.transmissions.append(value)

    def close(self) -> None:
        self._event("close")
        self.closed = True


def test_success_uses_exact_uri_finite_prefix_ladder_and_finishes_muted() -> None:
    device = FakeTx2()
    opened: list[str] = []
    requested = plan(
        Tx2LadderStep(80, waveform(16)),
        Tx2LadderStep(70, waveform(16)),
        Tx2LadderStep(70, waveform(32)),
    )

    def factory(uri: str) -> FakeTx2:
        opened.append(uri)
        return device

    result = run_conducted_tx2_ladder(requested, factory)

    assert opened == [URI]
    assert len(device.transmissions) == 3
    assert [step.tx_attenuation_db for step in result.steps] == [80, 70, 70]
    assert [step.waveform_rms_counts for step in result.steps] == [16, 16, 32]
    assert result.final_state_verified_muted
    assert device.gain == -80.0
    assert set(device.dds.values()) == {0.0}
    assert device.closed
    assert device.events[-1] == "close"
    assert device.events.index("attest_qualified_v5") < device.events.index(
        "destroy_tx_buffer"
    )


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda value: replace(value, uri=""), "exact standard-libiio"),
        (lambda value: replace(value, uri="ip:"), "exact standard-libiio"),
        (lambda value: replace(value, uri="ip:radio host"), "exact standard-libiio"),
        (lambda value: replace(value, uri="local:"), "exact standard-libiio"),
        (
            lambda value: replace(value, armed_radio_serial="other"),
            "explicitly armed",
        ),
        (
            lambda value: replace(
                value, topology=replace(value.topology, radio_serial="other")
            ),
            "different radio serial",
        ),
        (
            lambda value: replace(
                value,
                expected_radio=replace(value.expected_radio, serial="other"),
            ),
            "attestation names a different serial",
        ),
        (
            lambda value: replace(
                value,
                expected_radio=replace(value.expected_radio, channel_count=1),
            ),
            "qualified V5 2RX/2TX layout",
        ),
        (
            lambda value: replace(
                value, topology=replace(value.topology, topology="TX2->RX1")
            ),
            "exact conducted",
        ),
        (
            lambda value: replace(
                value, topology=replace(value.topology, confirmation="yes")
            ),
            "antenna-free",
        ),
        (
            lambda value: replace(
                value, topology=replace(value.topology, splitter_id=" ")
            ),
            "splitter/tee identity",
        ),
        (
            lambda value: replace(
                value,
                topology=replace(value.topology, tx2_to_rx1_attenuator_ids=()),
            ),
            "identified attenuators",
        ),
        (
            lambda value: replace(
                value, topology=replace(value.topology, tx2_to_rx2_attenuation_db=29)
            ),
            "at least 30 dB",
        ),
        (lambda value: replace(value, tx_lo_hz=1), "LO lies outside"),
        (lambda value: replace(value, sample_rate_hz=999_999), "sample rate lies"),
        (lambda value: replace(value, steps=()), "invalid number of steps"),
        (
            lambda value: replace(value, steps=(Tx2LadderStep(70, waveform()),)),
            "must start",
        ),
        (
            lambda value: replace(
                value,
                steps=(
                    Tx2LadderStep(80, waveform()),
                    Tx2LadderStep(60, waveform()),
                ),
            ),
            "exactly one",
        ),
        (
            lambda value: replace(
                value,
                steps=(
                    Tx2LadderStep(80, waveform()),
                    Tx2LadderStep(70, waveform()),
                    Tx2LadderStep(80, waveform()),
                ),
            ),
            "exactly one",
        ),
        (
            lambda value: replace(
                value,
                steps=(
                    Tx2LadderStep(80, waveform()),
                    Tx2LadderStep(70, waveform(32)),
                ),
            ),
            "exactly one",
        ),
        (
            lambda value: replace(value, steps=(Tx2LadderStep(80, waveform(24)),)),
            "fixed safety ladder",
        ),
    ],
)
def test_static_safety_gate_fails_before_opening_context(
    mutate: Callable[[ConductedTx2Plan], ConductedTx2Plan], message: str
) -> None:
    opened = False

    def factory(uri: str) -> FakeTx2:
        del uri
        nonlocal opened
        opened = True
        return FakeTx2()

    with pytest.raises(Tx2SafetyError, match=message):
        run_conducted_tx2_ladder(mutate(plan()), factory)
    assert not opened


@pytest.mark.parametrize(
    "bad_waveform, message",
    [
        (FiniteTx2Waveform(b"", 16, hashlib.sha256(b"").hexdigest()), "nonempty"),
        (FiniteTx2Waveform(b"abc", 16, hashlib.sha256(b"abc").hexdigest()), "CI16"),
        (
            replace(waveform(), sha256="0" * 64),
            "digest",
        ),
        (
            FiniteTx2Waveform.from_ci16(
                struct.pack("<hh", 1, 0) * 64, declared_rms_counts=16
            ),
            "RMS",
        ),
        (
            FiniteTx2Waveform.from_ci16(
                struct.pack("<hh", 513, 0) + struct.pack("<hh", 0, 0) * 1027,
                declared_rms_counts=16,
            ),
            "component peak",
        ),
        (
            waveform(sample_count=MAXIMUM_SAMPLE_COUNT + 1),
            "sample-count",
        ),
    ],
)
def test_waveform_safety_gate_fails_before_opening_context(
    bad_waveform: FiniteTx2Waveform, message: str
) -> None:
    opened = False

    def factory(uri: str) -> FakeTx2:
        del uri
        nonlocal opened
        opened = True
        return FakeTx2()

    with pytest.raises(Tx2SafetyError, match=message):
        run_conducted_tx2_ladder(
            replace(plan(), steps=(Tx2LadderStep(80, bad_waveform),)), factory
        )
    assert not opened


def test_wrong_selected_serial_is_never_mutated_and_context_is_closed() -> None:
    device = FakeTx2(serial="some-other-radio")

    with pytest.raises(Tx2SafetyError, match="selected radio serial"):
        run_conducted_tx2_ladder(plan(), lambda uri: device)

    assert device.events == ["attest_qualified_v5", "read_serial", "close"]
    assert device.gain == -10.0
    assert set(device.dds.values()) == {0.5}
    assert not device.transmissions


def test_device_factory_failure_is_propagated_without_retry_or_fallback() -> None:
    calls: list[str] = []

    def unavailable(uri: str) -> FakeTx2:
        calls.append(uri)
        raise OSError("exact context unavailable")

    with pytest.raises(OSError, match="exact context unavailable"):
        run_conducted_tx2_ladder(plan(), unavailable)
    assert calls == [URI]


def test_v5_attestation_failure_only_closes_unverified_context() -> None:
    device = FakeTx2(fail_once="attest_qualified_v5")

    with pytest.raises(OSError, match="attest_qualified_v5"):
        run_conducted_tx2_ladder(plan(), lambda uri: device)

    assert device.events == ["attest_qualified_v5", "close"]
    assert device.gain == -10.0
    assert set(device.dds.values()) == {0.5}
    assert not device.transmissions


@pytest.mark.parametrize(
    "failure",
    [
        "destroy_tx_buffer",
        "disable_tx2_dds",
        "set_tx2_gain_db",
        "read_tx2_gain_db",
        "read_tx2_dds_scales",
        "set_tx2_lo_hz",
        "read_tx2_lo_hz",
        "set_sample_rate_hz",
        "read_sample_rate_hz",
        "transmit_tx2_finite_ci16",
    ],
)
def test_every_post_identity_failure_still_mutes_verifies_and_closes(
    failure: str,
) -> None:
    device = FakeTx2(fail_once=failure)

    with pytest.raises((OSError, Tx2SafetyError)):
        run_conducted_tx2_ladder(plan(), lambda uri: device)

    assert device.gain == -80.0
    assert set(device.dds.values()) == {0.0}
    assert device.closed
    assert device.events[-1] == "close"
    assert "destroy_tx_buffer" in device.events
    assert "disable_tx2_dds" in device.events


def test_readback_mismatch_refuses_transmit_and_finishes_muted() -> None:
    device = FakeTx2()

    def wrong_lo() -> float:
        device._event("read_tx2_lo_hz")
        return device.lo + 3

    device.read_tx2_lo_hz = wrong_lo  # type: ignore[method-assign]
    with pytest.raises(Tx2SafetyError, match="LO readback"):
        run_conducted_tx2_ladder(plan(), lambda uri: device)
    assert not device.transmissions
    assert device.gain == -80.0
    assert set(device.dds.values()) == {0.0}
    assert device.closed


@pytest.mark.parametrize("readback", ["gain", "sample_rate"])
def test_other_control_readback_mismatches_refuse_transmit(readback: str) -> None:
    device = FakeTx2()
    if readback == "gain":
        calls = 0

        def wrong_gain_on_step() -> float:
            nonlocal calls
            calls += 1
            device._event("read_tx2_gain_db")
            return device.gain + (1 if calls == 3 else 0)

        device.read_tx2_gain_db = wrong_gain_on_step  # type: ignore[method-assign]
    else:
        calls = 0

        def wrong_rate_once() -> float:
            nonlocal calls
            calls += 1
            device._event("read_sample_rate_hz")
            return device.rate + (3 if calls == 1 else 0)

        device.read_sample_rate_hz = wrong_rate_once  # type: ignore[method-assign]

    with pytest.raises(Tx2SafetyError, match="readback differs"):
        run_conducted_tx2_ladder(plan(), lambda uri: device)
    assert not device.transmissions
    assert device.gain == -80.0
    assert set(device.dds.values()) == {0.0}
    assert device.closed


def test_incomplete_or_nonzero_dds_readback_refuses_transmit() -> None:
    for scales in (
        {"altvoltage4": 0.0},
        {
            "altvoltage4": 0.0,
            "altvoltage5": 0.0,
            "altvoltage6": 0.0,
            "altvoltage7": 0.1,
        },
    ):
        device = FakeTx2()
        calls = 0

        def bad_readback(
            selected: FakeTx2 = device,
            selected_scales: dict[str, float] = scales,
        ) -> dict[str, float]:
            nonlocal calls
            calls += 1
            selected._event("read_tx2_dds_scales")
            return selected_scales if calls == 1 else dict(selected.dds)

        device.read_tx2_dds_scales = bad_readback  # type: ignore[method-assign]

        def select(_uri: str, selected: FakeTx2 = device) -> FakeTx2:
            return selected

        with pytest.raises(Tx2SafetyError, match="DDS mute readback"):
            run_conducted_tx2_ladder(plan(), select)
        assert not device.transmissions
        assert device.closed


def test_cleanup_attempts_every_action_and_surfaces_failure() -> None:
    device = FakeTx2(fail_always="disable_tx2_dds")

    with pytest.raises(Tx2CleanupError, match="disable TX2 DDS"):
        run_conducted_tx2_ladder(plan(), lambda uri: device)

    assert "set_tx2_gain_db" in device.events
    assert "read_tx2_gain_db" in device.events
    assert "read_tx2_dds_scales" in device.events
    assert device.closed
    assert device.events[-1] == "close"


def test_close_failure_is_a_cleanup_failure_after_verified_mute() -> None:
    device = FakeTx2(fail_always="close")

    with pytest.raises(Tx2CleanupError, match="close exact context"):
        run_conducted_tx2_ladder(plan(), lambda uri: device)

    assert device.gain == -80.0
    assert set(device.dds.values()) == {0.0}
    assert device.events[-1] == "close"


def test_serial_observation_failure_only_closes_unverified_context() -> None:
    device = FakeTx2(fail_once="read_serial")

    with pytest.raises(OSError, match="read_serial"):
        run_conducted_tx2_ladder(plan(), lambda uri: device)

    assert device.events == ["attest_qualified_v5", "read_serial", "close"]


class _Array:
    def __init__(self, values: Sequence[complex]) -> None:
        self.values = list(values)

    def reshape(self, shape: tuple[int, int]) -> _Array:
        assert shape[1] == 2
        return self

    def __getitem__(self, key: tuple[slice, int]) -> _Array:
        column = key[1]
        return _Array(self.values[column::2])

    def astype(self, dtype: object) -> _Array:
        del dtype
        return self

    def __mul__(self, other: complex) -> _Array:
        return _Array([other * value for value in self.values])

    def __rmul__(self, other: complex) -> _Array:
        return self * other

    def __add__(self, other: _Array) -> _Array:
        return _Array([a + b for a, b in zip(self.values, other.values, strict=True)])

    def __len__(self) -> int:
        return len(self.values)


class _StrictPinnedPyadiRadio:
    """Only the attributes exposed by the pinned pyadi 0.0.21 TX interface."""

    __slots__ = (
        "_ctx",
        "close_calls",
        "sample_rate",
        "tx_cyclic_buffer",
        "tx_enabled_channels",
        "tx_hardwaregain_chan1",
        "tx_lo",
        "tx_values",
    )

    def __init__(self, context: object, tx_values: list[_Array]) -> None:
        self._ctx = context
        self.close_calls = 0
        self.sample_rate = 0
        self.tx_cyclic_buffer = True
        self.tx_enabled_channels = [0, 1]
        self.tx_hardwaregain_chan1 = -80.0
        self.tx_lo = 0
        self.tx_values = tx_values

    def tx_destroy_buffer(self) -> None:
        return None

    def tx(self, values: _Array) -> None:
        self.tx_values.append(values)

    def close(self) -> None:
        self.close_calls += 1
        self._ctx = None


def test_pyadi_adapter_passes_exact_uri_selects_tx2_and_forces_finite_mode() -> None:
    opened: list[str] = []
    timeout_calls: list[int] = []
    tx_values: list[_Array] = []
    dds_channels = [
        SimpleNamespace(
            id=name, output=True, attrs={"scale": SimpleNamespace(value="1")}
        )
        for name in (
            "altvoltage4",
            "altvoltage5",
            "altvoltage6",
            "altvoltage7",
        )
    ]
    context = SimpleNamespace(
        attrs={"hw_serial": SERIAL},
        find_device=lambda name: (
            SimpleNamespace(channels=dds_channels)
            if name == "cf-ad9361-dds-core-lpc"
            else None
        ),
        set_timeout=timeout_calls.append,
    )
    radio = _StrictPinnedPyadiRadio(context, tx_values)
    observed_devices: list[object] = []

    def observe_radio(value: object) -> ObservedV5Radio:
        observed_devices.append(value)
        return OBSERVED_RADIO

    def open_radio(*, uri: str) -> object:
        opened.append(uri)
        return radio

    adi = SimpleNamespace(ad9361=open_radio)
    numpy = SimpleNamespace(
        float32="float32",
        frombuffer=lambda value, dtype: _Array(
            list(struct.unpack(f"<{len(value) // 2}h", value))
        ),
    )
    loaded = {"adi": adi, "numpy": numpy}

    device = PyadiTx2Device(
        URI,
        module_loader=loaded.__getitem__,
        runtime_observer=lambda: OBSERVED_RUNTIME,
        radio_observer=observe_radio,
    )
    device.attest_qualified_v5(URI, EXPECTED_RUNTIME, EXPECTED_RADIO)
    device.disable_tx2_dds()
    device.transmit_tx2_finite_ci16(waveform().ci16_le)

    assert opened == [URI]
    assert timeout_calls == [IO_TIMEOUT_MS]
    assert observed_devices == [radio]
    assert radio.tx_enabled_channels == [1]
    assert radio.tx_cyclic_buffer is False
    assert not hasattr(radio, "tx_buffer_size")
    unsupported_attribute = "tx_buffer_size"
    with pytest.raises(AttributeError):
        setattr(radio, unsupported_attribute, 64)
    assert len(tx_values) == 1
    assert set(device.read_tx2_dds_scales().values()) == {0.0}

    device.close()
    assert device._device is None
    assert device._context is None
    assert radio._ctx is None
    assert radio.close_calls == 1


def test_pyadi_adapter_rejects_unqualified_runtime_before_tx_mutation() -> None:
    context = SimpleNamespace(set_timeout=lambda value: None)
    radio = _StrictPinnedPyadiRadio(context, [])
    loaded = {
        "adi": SimpleNamespace(ad9361=lambda *, uri: radio),
        "numpy": SimpleNamespace(),
    }
    device = PyadiTx2Device(
        URI,
        module_loader=loaded.__getitem__,
        runtime_observer=lambda: replace(OBSERVED_RUNTIME, pyadi_version="0.0.20"),
        radio_observer=lambda value: OBSERVED_RADIO,
    )

    with pytest.raises(Tx2SafetyError, match="qualified V5 host/radio attestation"):
        device.attest_qualified_v5(URI, EXPECTED_RUNTIME, EXPECTED_RADIO)

    assert radio.tx_hardwaregain_chan1 == -80.0
    assert radio.tx_values == []
    device.close()


@pytest.mark.parametrize(
    "observed_radio",
    [
        replace(OBSERVED_RADIO, firmware_release="some-other-firmware"),
        replace(OBSERVED_RADIO, channel_count=1),
    ],
)
def test_pyadi_adapter_rejects_wrong_firmware_or_hardware_mode_before_mutation(
    observed_radio: ObservedV5Radio,
) -> None:
    context = SimpleNamespace(set_timeout=lambda value: None)
    radio = _StrictPinnedPyadiRadio(context, [])
    loaded = {
        "adi": SimpleNamespace(ad9361=lambda *, uri: radio),
        "numpy": SimpleNamespace(),
    }
    device = PyadiTx2Device(
        URI,
        module_loader=loaded.__getitem__,
        runtime_observer=lambda: OBSERVED_RUNTIME,
        radio_observer=lambda value: observed_radio,
    )

    with pytest.raises(Tx2SafetyError, match="qualified V5 host/radio attestation"):
        device.attest_qualified_v5(URI, EXPECTED_RUNTIME, EXPECTED_RADIO)

    assert radio.tx_hardwaregain_chan1 == -80.0
    assert radio.tx_values == []
    device.close()


def test_pyadi_adapter_closes_device_when_context_is_absent() -> None:
    closed = False

    def close() -> None:
        nonlocal closed
        closed = True

    radio = SimpleNamespace(close=close)
    loaded = {
        "adi": SimpleNamespace(ad9361=lambda *, uri: radio),
        "numpy": SimpleNamespace(),
    }

    with pytest.raises(Tx2SafetyError, match="no libiio context"):
        PyadiTx2Device(URI, module_loader=loaded.__getitem__)
    assert closed


def test_all_allowed_levels_have_a_valid_hardware_free_fixture() -> None:
    for level in ALLOWED_WAVEFORM_RMS_COUNTS:
        candidate = waveform(level)
        assert candidate.declared_rms_counts == level


def test_capture_component_has_no_conducted_tx_fixture_dependency() -> None:
    capture_root = Path(__file__).resolve().parents[2] / "src" / "leo_flow" / "capture"
    for source in capture_root.rglob("*.py"):
        assert "leo_flow.fixtures" not in source.read_text(encoding="utf-8"), source
