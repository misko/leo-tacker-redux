"""Lazy pyadi-iio adapter for paired Pluto receive capture."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeAlias, cast

from leo_flow.contracts._validation import freeze_mapping
from leo_flow.contracts.capture import (
    GainMode,
    GainSetting,
    SegmentManifest,
    SegmentRequest,
)
from leo_flow.contracts.continuity import (
    CaptureProvenance,
    ContinuityPolicy,
    RefillMetadata,
)
from leo_flow.contracts.core import RadioId, ReceiverChainId, UtcNs

from ..clock import CaptureClock, SystemCaptureClock
from ..errors import (
    RadioConfigurationError,
    RadioDisconnectedError,
    ReceiverSkewError,
    RefillError,
    SampleCountError,
    TuningError,
)

CI16_BYTES_PER_COMPONENT = 2
IQ_COMPONENTS = 2
PAIRED_RECEIVERS = 2


class PlutoDevice(Protocol):
    rx_enabled_channels: Sequence[int]
    sample_rate: int
    rx_rf_bandwidth: int
    rx_lo: int
    rx_buffer_size: int
    rx_output_type: str

    def _rx_buffered_data(self) -> object: ...


DeviceFactory: TypeAlias = Callable[[str], PlutoDevice]
Interleaver: TypeAlias = Callable[[object, int], bytes]
SerialReader: TypeAlias = Callable[[PlutoDevice], str | None]
HealthReader: TypeAlias = Callable[[PlutoDevice], Mapping[str, int | bool]]
MetadataReader: TypeAlias = Callable[
    [PlutoDevice, int, int], tuple[object, RefillMetadata]
]
TimeoutSetter: TypeAlias = Callable[[PlutoDevice, int], None]

V5_FIRMWARE_RELEASE = "v0.38-plutoplus-spf-libiio-metadata-v5"
V5_FIRMWARE_COMMIT = "d7c87a9a28094ee6f0b23cb47df9ff737b5a69d8"


@dataclass(frozen=True)
class PlutoRadioConfig:
    uri: str
    expected_serial: str
    radio_id: RadioId
    receiver_chain_ids: tuple[ReceiverChainId, ReceiverChainId]
    physical_rx_channels: tuple[int, int] = (0, 1)
    block_samples: int = 262_144
    agc_mode: str = "slow_attack"
    frequency_tolerance_hz: float = 1.0
    sample_rate_tolerance_hz: float = 1.0
    bandwidth_tolerance_hz: float = 1.0
    gain_tolerance_db: float = 0.25
    continuity_policy: ContinuityPolicy = ContinuityPolicy.REQUIRE_VERIFIED
    host_libiio_version: str = "unknown"
    firmware_release: str = V5_FIRMWARE_RELEASE
    firmware_commit: str = V5_FIRMWARE_COMMIT
    metadata_protocol: str = "spf-radio-metadata-v3"
    metadata_capability: str = "iio,buffer-metadata=1"
    io_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        if not self.uri or not self.expected_serial:
            raise ValueError("Pluto URI and expected serial are required")
        if (
            len(set(self.receiver_chain_ids)) != PAIRED_RECEIVERS
            or len(set(self.physical_rx_channels)) != PAIRED_RECEIVERS
        ):
            raise ValueError("Pluto capture requires two distinct receiver channels")
        if self.block_samples <= 0:
            raise ValueError("block_samples must be positive")
        if self.io_timeout_ms <= 0:
            raise ValueError("io_timeout_ms must be positive")
        if not self.agc_mode:
            raise ValueError("agc_mode cannot be empty")
        if not all(
            (
                self.host_libiio_version,
                self.firmware_release,
                self.firmware_commit,
                self.metadata_protocol,
                self.metadata_capability,
            )
        ):
            raise ValueError("v5 provenance fields cannot be empty")
        tolerances = (
            self.frequency_tolerance_hz,
            self.sample_rate_tolerance_hz,
            self.bandwidth_tolerance_hz,
            self.gain_tolerance_db,
        )
        if any(value < 0 for value in tolerances):
            raise ValueError("readback tolerances must be non-negative")


class PlutoPairedRadio:
    """One-context, one-refill paired RX implementation of `RadioDevice`."""

    def __init__(
        self,
        config: PlutoRadioConfig,
        *,
        device_factory: DeviceFactory | None = None,
        interleaver: Interleaver | None = None,
        serial_reader: SerialReader | None = None,
        health_reader: HealthReader | None = None,
        metadata_reader: MetadataReader | None = None,
        timeout_setter: TimeoutSetter | None = None,
        attested_provenance: CaptureProvenance | None = None,
        clock: CaptureClock | None = None,
    ) -> None:
        self.config = config
        self._device_factory = device_factory or _lazy_pluto_factory
        self._interleaver = interleaver or _lazy_numpy_interleaver
        self._serial_reader = serial_reader or _default_serial_reader
        self._health_reader = health_reader or _default_health_reader
        self._metadata_reader = metadata_reader
        self._timeout_setter = timeout_setter or set_libiio_timeout
        self._attested_provenance = attested_provenance
        self._clock = clock or SystemCaptureClock()
        self._device: PlutoDevice | None = None
        self._closed = False

    @property
    def radio_id(self) -> RadioId:
        return self.config.radio_id

    @property
    def continuity_policy(self) -> ContinuityPolicy:
        return self.config.continuity_policy

    @property
    def capture_provenance(self) -> CaptureProvenance:
        if self._attested_provenance is not None:
            return self._attested_provenance
        capability = (
            self.config.metadata_capability
            if self._metadata_reader is not None
            else "ordinary-buffer;continuity-unverified"
        )
        return CaptureProvenance(
            firmware_release=self.config.firmware_release,
            firmware_commit=self.config.firmware_commit,
            host_libiio_version=self.config.host_libiio_version,
            metadata_protocol=self.config.metadata_protocol,
            capability=capability,
        )

    def close(self) -> None:
        """Release metadata, receive buffer, and IIO context exactly once."""

        if self._closed:
            return
        self._closed = True
        device, self._device = self._device, None
        failures: list[BaseException] = []
        try:
            self._close_metadata_reader()
        except BaseException as error:  # noqa: BLE001 - close every owned resource
            failures.append(error)
        if device is not None:
            try:
                _destroy_receive_buffer(device)
            except BaseException as error:  # noqa: BLE001 - preserve first close failure
                failures.append(error)
            try:
                _close_device_context(device)
            except BaseException as error:  # noqa: BLE001 - close every owned resource
                failures.append(error)
        if failures:
            raise RadioDisconnectedError(
                f"Pluto shutdown failed: {type(failures[0]).__name__}"
            ) from failures[0]

    def acquire_segment_with_metadata(
        self,
        request: SegmentRequest,
        write_refill: Callable[[bytes, RefillMetadata | None], None],
    ) -> SegmentManifest:
        """Acquire paired CI16 and associate each write with normalized v5 facts.

        The injected reader is the narrow boundary to patched libiio. It must
        return ordinary pyadi native IQ plus a domain ``RefillMetadata``; no
        libiio/SPF wire object crosses this adapter boundary.
        """

        if self._metadata_reader is None:
            if self.continuity_policy is ContinuityPolicy.REQUIRE_VERIFIED:
                raise RadioConfigurationError(
                    "v5 contiguous capture requires a patched-libiio metadata reader"
                )
            return self.acquire_segment(request, lambda data: write_refill(data, None))
        provenance = self.capture_provenance
        if provenance.host_libiio_version == "unknown":
            raise RadioConfigurationError(
                "verified v5 capture requires exact host libiio provenance"
            )
        target_samples = _requested_sample_count(request)
        if target_samples % self.config.block_samples:
            raise RadioConfigurationError(
                "verified v5 capture sample count must align to the IIO block size"
            )
        self._validate_request(request)
        device = self._connect()
        try:
            self._configure(device, request)
        except RadioConfigurationError:
            raise
        except (OSError, RuntimeError) as error:
            raise TuningError(f"Pluto configuration failed: {error}") from error

        before_health = dict(self._health_reader(device))
        start_utc_ns = UtcNs(self._clock.now_utc_ns())
        monotonic_start_ns = self._clock.now_monotonic_ns()
        samples_written = 0
        refill_count = 0
        try:
            while samples_written < target_samples:
                native, metadata = self._metadata_reader(
                    device, refill_count, samples_written
                )
                encoded = self._interleaver(native, PAIRED_RECEIVERS)
                expected_bytes = (
                    self.config.block_samples
                    * PAIRED_RECEIVERS
                    * IQ_COMPONENTS
                    * CI16_BYTES_PER_COMPONENT
                )
                if len(encoded) != expected_bytes:
                    raise SampleCountError("v5 IQ refill has the wrong byte count")
                if (
                    metadata.refill_index != refill_count
                    or metadata.segment_sample_offset != samples_written
                    or metadata.sample_count != self.config.block_samples
                ):
                    raise RefillError("v5 metadata does not describe its IQ refill")
                write_refill(encoded, metadata)
                samples_written += metadata.sample_count
                refill_count += 1
        except (SampleCountError, ReceiverSkewError, RefillError):
            raise
        except (OSError, ConnectionError) as error:
            raise RadioDisconnectedError(
                f"Pluto receive disconnected: {error}"
            ) from error
        except Exception as error:
            raise RefillError(f"Pluto v5 receive failed: {error}") from error
        after_health = dict(self._health_reader(device))
        _validate_health(before_health, after_health)
        readback = self._readback(device, request)
        diagnostics = freeze_mapping(
            {
                "block_samples": self.config.block_samples,
                "byte_count": samples_written * 8,
                "continuity": "verified",
                "firmware_release": provenance.firmware_release,
                "host_libiio_version": provenance.host_libiio_version,
                "metadata_protocol": provenance.metadata_protocol,
                "refill_count": refill_count,
                "serial": self.config.expected_serial,
            },
            "diagnostics",
        )
        return SegmentManifest(
            segment_id=request.segment_id,
            requested=request,
            actual_center_frequency_hz=readback.center_frequency_hz,
            actual_sample_rate_hz=readback.sample_rate_hz,
            actual_bandwidth_hz=readback.bandwidth_hz,
            actual_gain=readback.actual_gain,
            start_utc_ns=start_utc_ns,
            monotonic_start_ns=monotonic_start_ns,
            sample_count=samples_written,
            shape=(samples_written, PAIRED_RECEIVERS, IQ_COMPONENTS),
            diagnostics=diagnostics,
        )

    def acquire_segment(
        self, request: SegmentRequest, write_ci16: Callable[[bytes], None]
    ) -> SegmentManifest:
        self._validate_request(request)
        device = self._connect()
        try:
            self._configure(device, request)
        except RadioConfigurationError:
            raise
        except (OSError, RuntimeError) as error:
            raise TuningError(f"Pluto configuration failed: {error}") from error

        target_samples = _requested_sample_count(request)
        before_health = dict(self._health_reader(device))
        start_utc_ns = UtcNs(self._clock.now_utc_ns())
        monotonic_start_ns = self._clock.now_monotonic_ns()
        samples_written = 0
        refill_count = 0
        byte_count = 0
        discarded_samples = 0
        try:
            while samples_written < target_samples:
                wanted = min(
                    self.config.block_samples, target_samples - samples_written
                )
                refill = device._rx_buffered_data()
                encoded = self._interleaver(refill, PAIRED_RECEIVERS)
                expected_bytes = (
                    self.config.block_samples
                    * PAIRED_RECEIVERS
                    * IQ_COMPONENTS
                    * CI16_BYTES_PER_COMPONENT
                )
                if len(encoded) != expected_bytes:
                    observed_samples = len(encoded) // (
                        PAIRED_RECEIVERS * IQ_COMPONENTS * CI16_BYTES_PER_COMPONENT
                    )
                    raise SampleCountError(
                        "Pluto refill returned "
                        f"{observed_samples} of {self.config.block_samples} paired samples"
                    )
                wanted_bytes = (
                    wanted * PAIRED_RECEIVERS * IQ_COMPONENTS * CI16_BYTES_PER_COMPONENT
                )
                write_ci16(encoded[:wanted_bytes])
                samples_written += wanted
                byte_count += wanted_bytes
                discarded_samples += self.config.block_samples - wanted
                refill_count += 1
        except (SampleCountError, ReceiverSkewError):
            raise
        except (OSError, ConnectionError) as error:
            raise RadioDisconnectedError(
                f"Pluto receive disconnected: {error}"
            ) from error
        except Exception as error:
            raise RefillError(f"Pluto receive failed: {error}") from error

        after_health = dict(self._health_reader(device))
        _validate_health(before_health, after_health)
        readback = self._readback(device, request)
        diagnostics = freeze_mapping(
            {
                "block_samples": self.config.block_samples,
                "byte_count": byte_count,
                "discarded_tail_samples": discarded_samples,
                "physical_rx_channels": list(self.config.physical_rx_channels),
                "refill_count": refill_count,
                "serial": self.config.expected_serial,
                "health_before": before_health,
                "health_after": after_health,
                "drop_telemetry_available": bool(after_health),
                "gain_readback_db": readback.gain_db,
            },
            "diagnostics",
        )
        return SegmentManifest(
            segment_id=request.segment_id,
            requested=request,
            actual_center_frequency_hz=readback.center_frequency_hz,
            actual_sample_rate_hz=readback.sample_rate_hz,
            actual_bandwidth_hz=readback.bandwidth_hz,
            actual_gain=readback.actual_gain,
            start_utc_ns=start_utc_ns,
            monotonic_start_ns=monotonic_start_ns,
            sample_count=samples_written,
            shape=(samples_written, PAIRED_RECEIVERS, IQ_COMPONENTS),
            diagnostics=diagnostics,
        )

    def _connect(self) -> PlutoDevice:
        if self._closed:
            raise RadioDisconnectedError("closed Pluto radio cannot acquire samples")
        if self._device is None:
            try:
                device = self._device_factory(self.config.uri)
            except (ImportError, OSError, RuntimeError) as error:
                raise RadioDisconnectedError(
                    f"cannot connect to Pluto at {self.config.uri}: {error}"
                ) from error
            try:
                self._timeout_setter(device, self.config.io_timeout_ms)
                serial = self._serial_reader(device)
                if serial != self.config.expected_serial:
                    raise RadioConfigurationError(
                        "Pluto serial mismatch: expected "
                        f"{self.config.expected_serial}, got {serial}"
                    )
                if not callable(getattr(device, "_rx_buffered_data", None)):
                    raise RadioConfigurationError(
                        "pyadi device lacks native paired CI16 refill capability"
                    )
            except RadioConfigurationError:
                _release_device(device)
                raise
            except (OSError, RuntimeError) as error:
                _release_device(device)
                raise RadioDisconnectedError(
                    f"Pluto I/O timeout configuration failed: {error}"
                ) from error
            self._device = device
        return self._device

    def _validate_request(self, request: SegmentRequest) -> None:
        if request.receiver_chain_ids != self.config.receiver_chain_ids:
            raise RadioConfigurationError(
                "segment receiver order differs from configured Pluto receiver order"
            )

    def _configure(self, device: PlutoDevice, request: SegmentRequest) -> None:
        self._close_metadata_reader()
        _destroy_receive_buffer(device)
        device.rx_enabled_channels = list(self.config.physical_rx_channels)
        device.rx_buffer_size = self.config.block_samples
        if hasattr(device, "rx_output_type"):
            device.rx_output_type = "raw"
        device.sample_rate = round(request.sample_rate_hz)
        device.rx_rf_bandwidth = round(request.bandwidth_hz)
        device.rx_lo = round(request.center_frequency_hz)
        for channel in self.config.physical_rx_channels:
            gain_mode_name = f"gain_control_mode_chan{channel}"
            gain_name = f"rx_hardwaregain_chan{channel}"
            if request.gain.mode is GainMode.MANUAL:
                setattr(device, gain_mode_name, "manual")
                assert request.gain.gain_db is not None
                setattr(device, gain_name, request.gain.gain_db)
            else:
                setattr(device, gain_mode_name, self.config.agc_mode)
        self._validate_readback(device, request)

    def _close_metadata_reader(self) -> None:
        close = getattr(self._metadata_reader, "close", None)
        if callable(close):
            close()

    def _validate_readback(self, device: PlutoDevice, request: SegmentRequest) -> None:
        _within(
            float(device.rx_lo),
            request.center_frequency_hz,
            self.config.frequency_tolerance_hz,
            "center frequency",
        )
        _within(
            float(device.sample_rate),
            request.sample_rate_hz,
            self.config.sample_rate_tolerance_hz,
            "sample rate",
        )
        _within(
            float(device.rx_rf_bandwidth),
            request.bandwidth_hz,
            self.config.bandwidth_tolerance_hz,
            "bandwidth",
        )
        if tuple(device.rx_enabled_channels) != self.config.physical_rx_channels:
            raise RadioConfigurationError("enabled-channel readback mismatch")
        for channel in self.config.physical_rx_channels:
            mode = getattr(device, f"gain_control_mode_chan{channel}")
            expected_mode = (
                "manual"
                if request.gain.mode is GainMode.MANUAL
                else self.config.agc_mode
            )
            if mode != expected_mode:
                raise RadioConfigurationError(
                    f"gain mode readback mismatch on channel {channel}"
                )
            if request.gain.mode is GainMode.MANUAL:
                assert request.gain.gain_db is not None
                _within(
                    float(getattr(device, f"rx_hardwaregain_chan{channel}")),
                    request.gain.gain_db,
                    self.config.gain_tolerance_db,
                    f"gain channel {channel}",
                )

    def _readback(self, device: PlutoDevice, request: SegmentRequest) -> _Readback:
        gain_db: list[float] = []
        if request.gain.mode is GainMode.MANUAL:
            gain_db = [
                float(getattr(device, f"rx_hardwaregain_chan{channel}"))
                for channel in self.config.physical_rx_channels
            ]
            actual_gain = GainSetting(GainMode.MANUAL, sum(gain_db) / len(gain_db))
        else:
            actual_gain = GainSetting(GainMode.AGC)
        return _Readback(
            center_frequency_hz=float(device.rx_lo),
            sample_rate_hz=float(device.sample_rate),
            bandwidth_hz=float(device.rx_rf_bandwidth),
            actual_gain=actual_gain,
            gain_db=tuple(gain_db),
        )


@dataclass(frozen=True)
class _Readback:
    center_frequency_hz: float
    sample_rate_hz: float
    bandwidth_hz: float
    actual_gain: GainSetting
    gain_db: tuple[float, ...]


def _requested_sample_count(request: SegmentRequest) -> int:
    if request.sample_count is not None:
        return request.sample_count
    assert request.duration_s is not None
    result = round(request.duration_s * request.sample_rate_hz)
    if result <= 0:
        raise SampleCountError("duration resolves to no samples")
    return result


def _within(actual: float, requested: float, tolerance: float, name: str) -> None:
    if abs(actual - requested) > tolerance:
        raise RadioConfigurationError(
            f"{name} readback mismatch: requested {requested}, got {actual}"
        )


def _validate_health(
    before: Mapping[str, int | bool], after: Mapping[str, int | bool]
) -> None:
    for key, after_value in after.items():
        before_value = before.get(key, False if isinstance(after_value, bool) else 0)
        if isinstance(after_value, bool):
            bad = after_value and not bool(before_value)
        else:
            bad = int(after_value) > int(before_value)
        if bad:
            raise RefillError(f"Pluto health counter increased: {key}")


def _lazy_pluto_factory(uri: str) -> PlutoDevice:
    try:
        adi = importlib.import_module("adi")
    except ImportError as error:
        raise ImportError(
            "Pluto capture requires the hardware extra: install leo-tracker-redux[hardware]"
        ) from error
    try:
        device_type = adi.ad9361
    except AttributeError as error:
        raise ImportError(
            "pyadi-iio does not provide the AD9361 device adapter"
        ) from error
    return cast(PlutoDevice, device_type(uri=uri))


def set_libiio_timeout(device: PlutoDevice, timeout_ms: int) -> None:
    """Install the finite timeout used by every subsequent context operation."""

    for name in ("ctx", "_ctx"):
        context = getattr(device, name, None)
        setter = getattr(context, "set_timeout", None)
        if callable(setter):
            setter(timeout_ms)
            return
    raise RadioConfigurationError(
        "Pluto device does not expose a libiio context timeout"
    )


def _destroy_receive_buffer(device: PlutoDevice) -> None:
    destroy = getattr(device, "rx_destroy_buffer", None)
    if callable(destroy):
        destroy()


def _close_device_context(device: PlutoDevice) -> None:
    close = getattr(device, "close", None)
    if callable(close):
        close()


def _release_device(device: PlutoDevice) -> None:
    try:
        _destroy_receive_buffer(device)
    finally:
        _close_device_context(device)


def _lazy_numpy_interleaver(refill: object, expected_channels: int) -> bytes:
    try:
        np = importlib.import_module("numpy")
    except ImportError as error:
        raise ImportError(
            "Pluto capture requires NumPy from the hardware extra"
        ) from error
    expected_components = expected_channels * IQ_COMPONENTS
    if not isinstance(refill, (list, tuple)) or len(refill) != expected_components:
        raise ReceiverSkewError(
            f"Pluto native refill must contain exactly {expected_components} components"
        )
    components = [np.asarray(component) for component in refill]
    if any(component.ndim != 1 for component in components):
        raise ReceiverSkewError("Pluto native components must be one-dimensional")
    lengths = tuple(int(component.shape[0]) for component in components)
    if len(set(lengths)) != 1:
        raise ReceiverSkewError(f"Pluto native component lengths differ: {lengths}")
    if any(
        component.dtype.kind != "i"
        or component.dtype.itemsize != CI16_BYTES_PER_COMPONENT
        for component in components
    ):
        raise SampleCountError("Pluto native components must be signed int16")
    samples = lengths[0]
    output = np.empty((samples, expected_channels, IQ_COMPONENTS), dtype="<i2")
    for receiver_index in range(expected_channels):
        for component_index in range(IQ_COMPONENTS):
            source = components[receiver_index * IQ_COMPONENTS + component_index]
            output[:, receiver_index, component_index] = source
    return cast(bytes, output.tobytes(order="C"))


def _default_serial_reader(device: PlutoDevice) -> str | None:
    direct = getattr(device, "serial", None)
    if direct is not None:
        return str(direct)
    for owner_name in ("ctx", "_ctx", "_ctrl"):
        owner = getattr(device, owner_name, None)
        attrs = getattr(owner, "attrs", None)
        if attrs is None:
            continue
        for key in ("hw_serial", "serial"):
            try:
                raw_value = attrs[key]
            except (KeyError, AttributeError, TypeError):
                continue
            value = getattr(raw_value, "value", raw_value)
            if value:
                return str(value)
    return None


def _default_health_reader(device: PlutoDevice) -> Mapping[str, int | bool]:
    result: dict[str, int | bool] = {}
    for name in ("rx_dropped_samples", "rx_overflow", "overflow"):
        try:
            value = getattr(device, name, None)
        except (OSError, RuntimeError):
            continue
        if isinstance(value, (bool, int)):
            result[name] = value
    return result
