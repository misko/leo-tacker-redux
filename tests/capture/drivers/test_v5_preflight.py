from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import pytest

from leo_flow.capture.drivers.pluto import PlutoRadioConfig
from leo_flow.capture.drivers.v5_preflight import (
    ExpectedV5Radio,
    ExpectedV5Runtime,
    ObservedV5Radio,
    ObservedV5Runtime,
    StandardLibiioTransport,
    attest_v5,
    create_attested_v5_radio,
    standard_libiio_transport,
)
from leo_flow.capture.errors import RadioConfigurationError
from leo_flow.contracts.continuity import RefillMetadata
from leo_flow.contracts.core import RadioId, ReceiverChainId

IIO_COMMIT = "c26258bfa33098c2b215e19cf85d448e89499b1a"
SPF_COMMIT = "c40ee4116546889effd72056115adaaa1bc3fd40"
FIRMWARE = "v0.38-plutoplus-spf-libiio-metadata-v5"
FIRMWARE_COMMIT = "d7c87a9a28094ee6f0b23cb47df9ff737b5a69d8"


def expected_runtime() -> ExpectedV5Runtime:
    return ExpectedV5Runtime(
        runtime_id="pluto-v5-libiio-0.25-spfmeta3",
        schema="leo-flow.v5-runtime/v1",
        iio_module_path="/usr/local/lib/python3.11/dist-packages/iio.py",
        iio_version=(0, 25, "c26258b"),
        iio_commit=IIO_COMMIT,
        native_libiio_prefix="/opt/leo-v5",
        required_backends=frozenset(("local", "ip", "usb")),
        pyadi_version="0.0.21",
        pyadi_module_path="/usr/local/lib/python3.11/dist-packages/adi/__init__.py",
        spf_module_path=(
            "/usr/local/lib/python3.11/dist-packages/spf/direct_radio/iio_metadata.py"
        ),
        spf_revision=SPF_COMMIT,
        spf_import="spf.direct_radio.iio_metadata:IioMetadataRx",
        metadata_protocol="spf-radio-metadata-v3",
    )


def observed_runtime() -> ObservedV5Runtime:
    expected = expected_runtime()
    return ObservedV5Runtime(
        runtime_id=expected.runtime_id,
        schema=expected.schema,
        iio_module_path=expected.iio_module_path,
        iio_version=expected.iio_version,
        iio_commit=expected.iio_commit,
        metadata_buffer_present=True,
        native_libiio_paths=("/opt/leo-v5/lib/libiio.so.0",),
        available_backends=frozenset(("local", "ip", "usb")),
        pyadi_version=expected.pyadi_version,
        pyadi_module_path=expected.pyadi_module_path,
        spf_module_path=expected.spf_module_path,
        spf_revision=expected.spf_revision,
        spf_import=expected.spf_import,
        metadata_protocol=expected.metadata_protocol,
    )


def expected_radio() -> ExpectedV5Radio:
    return ExpectedV5Radio("serial-v5", FIRMWARE, FIRMWARE_COMMIT)


def observed_radio() -> ObservedV5Radio:
    return ObservedV5Radio(
        "serial-v5",
        FIRMWARE,
        "iio,buffer-metadata=1",
        0x0F,
        2,
        ("I0", "Q0", "I1", "Q1"),
    )


def test_matching_ip_and_usb_attestations_derive_observed_provenance() -> None:
    for uri, transport in (
        ("ip:192.0.2.1", StandardLibiioTransport.IP),
        ("usb:1.2.5", StandardLibiioTransport.USB),
    ):
        result = attest_v5(
            uri=uri,
            expected_runtime=expected_runtime(),
            observed_runtime=observed_runtime(),
            expected_radio=expected_radio(),
            observed_radio=observed_radio(),
        )
        assert result.transport is transport
        assert result.provenance.firmware_release == FIRMWARE
        assert result.provenance.host_libiio_version == "0.25.c26258b"
        assert result.provenance.capability == "iio,buffer-metadata=1"


@pytest.mark.parametrize("uri", ("direct-ip:host", "direct-usb:1.2", "local:"))
def test_custom_direct_and_local_transports_are_rejected(uri: str) -> None:
    with pytest.raises(RadioConfigurationError, match="separate adapter"):
        standard_libiio_transport(uri)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"runtime_id": "unknown-runtime"}, "runtime ID"),
        ({"schema": "leo-flow.v5-runtime/v2"}, "runtime schema"),
        ({"iio_module_path": "/venv/site-packages/iio.py"}, "iio module path"),
        ({"iio_version": (0, 25, "v0.25")}, "iio version"),
        ({"iio_commit": "ordinary-pypi"}, "iio commit"),
        ({"metadata_buffer_present": False}, "ordinary PyPI pylibiio"),
        (
            {"native_libiio_paths": ("/usr/lib/libiio.so.0",)},
            "native libiio paths",
        ),
        ({"native_libiio_paths": ()}, "native libiio paths"),
        ({"available_backends": frozenset(("local", "ip"))}, "backends absent"),
        ({"pyadi_version": "0.0.20"}, "pyadi version"),
        ({"pyadi_module_path": "/tmp/adi/__init__.py"}, "pyadi module path"),
        ({"spf_module_path": "/tmp/spf/iio_metadata.py"}, "SPF module path"),
        ({"spf_revision": "working-tree"}, "SPF revision"),
        ({"spf_import": "spf.direct_usb:Receiver"}, "SPF import"),
        ({"metadata_protocol": "spf-radio-metadata-v2"}, "metadata protocol"),
    ],
)
def test_host_runtime_mismatches_fail_closed(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(RadioConfigurationError, match=message):
        attest_v5(
            uri="ip:192.0.2.1",
            expected_runtime=expected_runtime(),
            observed_runtime=replace(observed_runtime(), **changes),
            expected_radio=expected_radio(),
            observed_radio=observed_radio(),
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"serial": "other"}, "radio serial"),
        ({"firmware_release": "v0.38-stock"}, "firmware release"),
        ({"metadata_capability": "0"}, "metadata capability"),
        ({"enabled_scan_mask": 0x03}, "enabled scan mask"),
        ({"channel_count": 1}, "paired channel count"),
        ({"component_layout": ("I0", "Q0")}, "component layout"),
    ],
)
def test_radio_mismatches_fail_closed(changes: dict[str, object], message: str) -> None:
    with pytest.raises(RadioConfigurationError, match=message):
        attest_v5(
            uri="usb:1.2.5",
            expected_runtime=expected_runtime(),
            observed_runtime=observed_runtime(),
            expected_radio=expected_radio(),
            observed_radio=replace(observed_radio(), **changes),
        )


class FakeDevice:
    serial = "serial-v5"
    rx_enabled_channels: Sequence[int] = ()
    sample_rate = 0
    rx_rf_bandwidth = 0
    rx_lo = 0
    rx_buffer_size = 0
    rx_output_type = "raw"

    def __init__(self) -> None:
        self.timeout_calls: list[int] = []
        self.destroy_calls = 0
        self._ctx = self

    def set_timeout(self, timeout_ms: int) -> None:
        self.timeout_calls.append(timeout_ms)

    def _rx_buffered_data(self) -> object:
        raise AssertionError("preflight test must not capture")

    def rx_destroy_buffer(self) -> None:
        self.destroy_calls += 1


def config(uri: str = "ip:192.0.2.1") -> PlutoRadioConfig:
    return PlutoRadioConfig(
        uri,
        "serial-v5",
        RadioId("radio_v5"),
        (ReceiverChainId("rx_1"), ReceiverChainId("rx_2")),
    )


def unused_metadata_reader(
    _device: object, _refill_index: int, _sample_offset: int
) -> tuple[object, RefillMetadata]:
    raise AssertionError("preflight test must not capture")


def test_composition_attests_before_returning_radio_and_uses_observed_provenance() -> (
    None
):
    device = FakeDevice()
    factory_calls: list[str] = []

    def factory(uri: str) -> FakeDevice:
        factory_calls.append(uri)
        return device

    def observe_device(seen: object) -> ObservedV5Radio:
        assert seen is device
        assert device.timeout_calls == [5_000]
        return observed_radio()

    caller_claims = replace(
        config(),
        firmware_release="caller-default-must-not-be-provenance",
        firmware_commit="caller-default-commit",
        host_libiio_version="unknown",
    )
    radio = create_attested_v5_radio(
        caller_claims,
        expected_runtime=expected_runtime(),
        expected_radio=expected_radio(),
        observe_runtime=observed_runtime,
        observe_radio=observe_device,
        device_factory=factory,
        metadata_reader=unused_metadata_reader,
    )
    assert factory_calls == ["ip:192.0.2.1"]
    assert device.timeout_calls == [5_000]
    assert radio.capture_provenance.firmware_release == FIRMWARE
    assert radio.capture_provenance.firmware_commit == FIRMWARE_COMMIT
    assert radio.capture_provenance.host_libiio_version == "0.25.c26258b"


def test_composition_does_not_open_device_when_host_fails() -> None:
    opened = False

    def factory(_uri: str) -> FakeDevice:
        nonlocal opened
        opened = True
        return FakeDevice()

    with pytest.raises(RadioConfigurationError, match="MetadataBuffer"):
        create_attested_v5_radio(
            config(),
            expected_runtime=expected_runtime(),
            expected_radio=expected_radio(),
            observe_runtime=lambda: replace(
                observed_runtime(), metadata_buffer_present=False
            ),
            observe_radio=lambda _device: observed_radio(),
            device_factory=factory,
            metadata_reader=unused_metadata_reader,
        )
    assert opened is False


def test_composition_releases_selected_device_when_radio_observation_fails() -> None:
    device = FakeDevice()

    def observe_device(_device: object) -> ObservedV5Radio:
        assert device.timeout_calls == [5_000]
        raise RuntimeError("radio unavailable")

    with pytest.raises(RuntimeError, match="radio unavailable"):
        create_attested_v5_radio(
            config(),
            expected_runtime=expected_runtime(),
            expected_radio=expected_radio(),
            observe_runtime=observed_runtime,
            observe_radio=observe_device,
            device_factory=lambda _uri: device,
            metadata_reader=unused_metadata_reader,
        )
    assert device.destroy_calls == 1
