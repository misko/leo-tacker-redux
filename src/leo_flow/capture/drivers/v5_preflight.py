"""Fail-closed startup attestation for the standard-libiio V5 adapter.

Runtime packaging owns how observations are collected.  This capture-owned
boundary compares immutable facts and releases no radio adapter until the host
and radio match the qualified manifest.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum

from leo_flow.contracts.continuity import CaptureProvenance

from ..errors import RadioConfigurationError
from .pluto import (
    DeviceFactory,
    HealthReader,
    Interleaver,
    MetadataReader,
    PlutoDevice,
    PlutoPairedRadio,
    PlutoRadioConfig,
)


class StandardLibiioTransport(str, Enum):
    IP = "standard_libiio_ip"
    USB = "standard_libiio_usb"


@dataclass(frozen=True, slots=True)
class ExpectedV5Runtime:
    runtime_id: str
    schema: str
    iio_module_path: str
    iio_version: tuple[int, int, str]
    iio_commit: str
    native_libiio_prefix: str
    required_backends: frozenset[str]
    pyadi_version: str
    pyadi_module_path: str
    spf_module_path: str
    spf_revision: str
    spf_import: str
    metadata_protocol: str

    def __post_init__(self) -> None:
        strings = (
            self.runtime_id,
            self.schema,
            self.iio_module_path,
            self.iio_commit,
            self.native_libiio_prefix,
            self.pyadi_version,
            self.pyadi_module_path,
            self.spf_module_path,
            self.spf_revision,
            self.spf_import,
            self.metadata_protocol,
        )
        if not all(strings) or not self.required_backends:
            raise ValueError("V5 runtime expectations cannot be empty")


@dataclass(frozen=True, slots=True)
class ObservedV5Runtime:
    runtime_id: str
    schema: str
    iio_module_path: str
    iio_version: tuple[int, int, str]
    iio_commit: str
    metadata_buffer_present: bool
    native_libiio_paths: tuple[str, ...]
    available_backends: frozenset[str]
    pyadi_version: str
    pyadi_module_path: str
    spf_module_path: str
    spf_revision: str
    spf_import: str
    metadata_protocol: str


@dataclass(frozen=True, slots=True)
class ExpectedV5Radio:
    serial: str
    firmware_release: str
    firmware_commit: str
    metadata_capability: str = "iio,buffer-metadata=1"
    enabled_scan_mask: int = 0x0F
    channel_count: int = 2
    component_layout: tuple[str, ...] = ("I0", "Q0", "I1", "Q1")

    def __post_init__(self) -> None:
        if not all(
            (
                self.serial,
                self.firmware_release,
                self.firmware_commit,
                self.metadata_capability,
            )
        ):
            raise ValueError("V5 radio expectations cannot be empty")


@dataclass(frozen=True, slots=True)
class ObservedV5Radio:
    serial: str
    firmware_release: str
    metadata_capability: str
    enabled_scan_mask: int
    channel_count: int
    component_layout: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class V5Attestation:
    transport: StandardLibiioTransport
    provenance: CaptureProvenance
    host: ObservedV5Runtime
    radio: ObservedV5Radio


RuntimeObserver = Callable[[], ObservedV5Runtime]
RadioObserver = Callable[[PlutoDevice], ObservedV5Radio]


def standard_libiio_transport(uri: str) -> StandardLibiioTransport:
    """Classify only standard libiio contexts; direct protocols are separate adapters."""

    if uri.startswith("ip:") and len(uri) > 3:
        return StandardLibiioTransport.IP
    if uri.startswith("usb:") and len(uri) > 4:
        return StandardLibiioTransport.USB
    raise RadioConfigurationError(
        "V5 Pluto adapter accepts only standard libiio ip: or usb: transports; "
        "custom direct transports require a separate adapter"
    )


def attest_v5(
    *,
    uri: str,
    expected_runtime: ExpectedV5Runtime,
    observed_runtime: ObservedV5Runtime,
    expected_radio: ExpectedV5Radio,
    observed_radio: ObservedV5Radio,
) -> V5Attestation:
    transport = standard_libiio_transport(uri)
    mismatches = _host_mismatches(transport, expected_runtime, observed_runtime)
    mismatches.extend(_radio_mismatches(expected_radio, observed_radio))
    if mismatches:
        raise RadioConfigurationError("V5 preflight failed: " + "; ".join(mismatches))

    provenance = CaptureProvenance(
        firmware_release=observed_radio.firmware_release,
        firmware_commit=expected_radio.firmware_commit,
        host_libiio_version=_version_text(observed_runtime.iio_version),
        metadata_protocol=observed_runtime.metadata_protocol,
        capability=observed_radio.metadata_capability,
    )
    return V5Attestation(transport, provenance, observed_runtime, observed_radio)


def _host_mismatches(
    transport: StandardLibiioTransport,
    expected_runtime: ExpectedV5Runtime,
    observed_runtime: ObservedV5Runtime,
) -> list[str]:
    required_backend = "ip" if transport is StandardLibiioTransport.IP else "usb"
    mismatches: list[str] = []
    _equal(
        mismatches,
        "runtime ID",
        observed_runtime.runtime_id,
        expected_runtime.runtime_id,
    )
    _equal(
        mismatches,
        "runtime schema",
        observed_runtime.schema,
        expected_runtime.schema,
    )
    _equal(
        mismatches,
        "iio module path",
        observed_runtime.iio_module_path,
        expected_runtime.iio_module_path,
    )
    _equal(
        mismatches,
        "iio version",
        observed_runtime.iio_version,
        expected_runtime.iio_version,
    )
    _equal(
        mismatches,
        "iio commit",
        observed_runtime.iio_commit,
        expected_runtime.iio_commit,
    )
    if not observed_runtime.metadata_buffer_present:
        mismatches.append(
            "iio.MetadataBuffer is absent (ordinary PyPI pylibiio is unsupported)"
        )
    prefix = expected_runtime.native_libiio_prefix.rstrip("/") + "/"
    if not observed_runtime.native_libiio_paths or any(
        not path.startswith(prefix) for path in observed_runtime.native_libiio_paths
    ):
        mismatches.append(
            "loaded native libiio paths are absent or outside approved prefix: "
            f"{observed_runtime.native_libiio_paths!r}"
        )
    missing_backends = (
        expected_runtime.required_backends - observed_runtime.available_backends
    )
    if missing_backends:
        mismatches.append(
            f"required libiio backends absent: {sorted(missing_backends)!r}"
        )
    if required_backend not in observed_runtime.available_backends:
        mismatches.append(f"selected transport backend absent: {required_backend}")
    _equal(
        mismatches,
        "pyadi version",
        observed_runtime.pyadi_version,
        expected_runtime.pyadi_version,
    )
    _equal(
        mismatches,
        "pyadi module path",
        observed_runtime.pyadi_module_path,
        expected_runtime.pyadi_module_path,
    )
    _equal(
        mismatches,
        "SPF module path",
        observed_runtime.spf_module_path,
        expected_runtime.spf_module_path,
    )
    _equal(
        mismatches,
        "SPF revision",
        observed_runtime.spf_revision,
        expected_runtime.spf_revision,
    )
    _equal(
        mismatches,
        "SPF import",
        observed_runtime.spf_import,
        expected_runtime.spf_import,
    )
    _equal(
        mismatches,
        "metadata protocol",
        observed_runtime.metadata_protocol,
        expected_runtime.metadata_protocol,
    )
    return mismatches


def _radio_mismatches(
    expected_radio: ExpectedV5Radio, observed_radio: ObservedV5Radio
) -> list[str]:
    mismatches: list[str] = []
    _equal(mismatches, "radio serial", observed_radio.serial, expected_radio.serial)
    _equal(
        mismatches,
        "firmware release",
        observed_radio.firmware_release,
        expected_radio.firmware_release,
    )
    _equal(
        mismatches,
        "metadata capability",
        observed_radio.metadata_capability,
        expected_radio.metadata_capability,
    )
    _equal(
        mismatches,
        "enabled scan mask",
        observed_radio.enabled_scan_mask,
        expected_radio.enabled_scan_mask,
    )
    _equal(
        mismatches,
        "paired channel count",
        observed_radio.channel_count,
        expected_radio.channel_count,
    )
    _equal(
        mismatches,
        "paired CI16 component layout",
        observed_radio.component_layout,
        expected_radio.component_layout,
    )
    return mismatches


def create_attested_v5_radio(
    config: PlutoRadioConfig,
    *,
    expected_runtime: ExpectedV5Runtime,
    expected_radio: ExpectedV5Radio,
    observe_runtime: RuntimeObserver,
    observe_radio: RadioObserver,
    device_factory: DeviceFactory,
    metadata_reader: MetadataReader,
    interleaver: Interleaver | None = None,
    health_reader: HealthReader | None = None,
) -> PlutoPairedRadio:
    """Run all startup gates before exposing a production capture device."""

    # Reject unsupported transports before loading hardware libraries or opening a radio.
    transport = standard_libiio_transport(config.uri)
    observed_runtime = observe_runtime()
    host_mismatches = _host_mismatches(transport, expected_runtime, observed_runtime)
    if host_mismatches:
        raise RadioConfigurationError(
            "V5 preflight failed: " + "; ".join(host_mismatches)
        )
    try:
        device = device_factory(config.uri)
    except (ImportError, OSError, RuntimeError) as error:
        raise RadioConfigurationError(
            f"V5 radio observation failed: {error}"
        ) from error
    observed_radio = observe_radio(device)
    attestation = attest_v5(
        uri=config.uri,
        expected_runtime=expected_runtime,
        observed_runtime=observed_runtime,
        expected_radio=expected_radio,
        observed_radio=observed_radio,
    )
    if config.expected_serial != observed_radio.serial:
        raise RadioConfigurationError("Pluto config serial differs from attested radio")
    used = False

    def attested_device_factory(uri: str) -> PlutoDevice:
        nonlocal used
        if used or uri != config.uri:
            raise RadioConfigurationError("attested V5 device cannot be rebound")
        used = True
        return device

    return PlutoPairedRadio(
        config,
        device_factory=attested_device_factory,
        interleaver=interleaver,
        health_reader=health_reader,
        metadata_reader=metadata_reader,
        attested_provenance=attestation.provenance,
    )


def _equal(
    mismatches: list[str], name: str, observed: object, expected: object
) -> None:
    if observed != expected:
        mismatches.append(
            f"{name} mismatch: expected {expected!r}, observed {observed!r}"
        )


def _version_text(version: Iterable[object]) -> str:
    return ".".join(str(part) for part in version)
