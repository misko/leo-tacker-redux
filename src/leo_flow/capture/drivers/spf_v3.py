"""Translate parsed SPF V3 observations into capture-domain contracts.

This boundary is deliberately structural: the optional patched-libiio host
package owns buffer creation and wire parsing.  The capture domain receives
only ordinary paired CI16 components and immutable normalized observations.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from typing import Protocol, TypeAlias, cast

from leo_flow.contracts.continuity import GainObservation, RefillFlag, RefillMetadata

from ..errors import RefillError
from .pluto import PlutoDevice


class SpfV3GainObservation(Protocol):
    sample_sequence_before: int
    sample_sequence_after: int
    read_duration_ns: int
    flags: int
    rx1_gain_db: int
    rx2_gain_db: int


class SpfV3Metadata(Protocol):
    flags: int
    stream_id: int
    buffer_sequence: int
    first_sample_sequence: int
    samples_per_channel: int
    iq_payload_bytes: int
    enabled_scan_mask: int
    channel_count: int
    rx1_gain_db_start: int
    rx2_gain_db_start: int
    rx1_gain_db_end: int
    rx2_gain_db_end: int
    rx1_rssi_start_qdb: int
    rx2_rssi_start_qdb: int
    rx1_rssi_end_qdb: int
    rx2_rssi_end_qdb: int
    gain_observation_overflow_count: int
    gain_event_overflow_count: int
    gain_observations: Sequence[SpfV3GainObservation]


class SpfV3CaptureSession(Protocol):
    def open(self) -> None: ...

    def capture(self) -> tuple[object, SpfV3Metadata, Mapping[str, object]]: ...

    def close(self) -> None: ...


class SpfV3SessionFactory(Protocol):
    def __call__(
        self, device: PlutoDevice, *, sample_rate_hz: int, samples_per_channel: int
    ) -> SpfV3CaptureSession: ...


NativeComponents: TypeAlias = tuple[object, object, object, object]

_START_VALID = 1 << 0
_END_VALID = 1 << 1
_SAMPLE_SEQUENCE_VALID = 1 << 4
_DEVICE_IIO_OVERFLOW = 1 << 11
_GAIN_READ_FAILED = 1 << 12
_FPGA_EVENT_OVERFLOW = 1 << 13
_RSSI_START_VALID = 1 << 15
_RSSI_END_VALID = 1 << 16
_RSSI_READ_FAILED = 1 << 17
_GAIN_DB_VALUES = 1 << 18
_GAIN_OBSERVATIONS_VALID = 1 << 19
_GAIN_OBSERVATION_OVERFLOW = 1 << 20
_HARDWARE_SAMPLE_COUNTER_VALID = 1 << 21
_OBSERVATION_VALID = 1 << 0
_OBSERVATION_INTERVAL_VALID = 1 << 1

_REQUIRED_FLAGS = (
    _START_VALID
    | _END_VALID
    | _SAMPLE_SEQUENCE_VALID
    | _RSSI_START_VALID
    | _RSSI_END_VALID
    | _GAIN_DB_VALUES
    | _GAIN_OBSERVATIONS_VALID
    | _HARDWARE_SAMPLE_COUNTER_VALID
)
_TIMING_KEYS = (
    "sample_time_monotonic_start_ns",
    "sample_time_monotonic_end_ns",
    "sample_time_realtime_start_ns",
    "sample_time_realtime_end_ns",
    "sample_time_uncertainty_ns",
)


class SpfV3MetadataReader:
    """Stateful callable compatible with ``PlutoPairedRadio.metadata_reader``.

    A new metadata buffer is opened at every segment boundary because Pluto
    configuration destroys the prior pyadi receive buffer.  The session
    factory is injected so neither this module nor the domain imports SPF.
    """

    def __init__(self, session_factory: SpfV3SessionFactory) -> None:
        self._session_factory = session_factory
        self._session: SpfV3CaptureSession | None = None

    def __call__(
        self, device: PlutoDevice, refill_index: int, segment_sample_offset: int
    ) -> tuple[NativeComponents, RefillMetadata]:
        if refill_index == 0:
            self.close()
            self._session = self._session_factory(
                device,
                sample_rate_hz=int(device.sample_rate),
                samples_per_channel=int(device.rx_buffer_size),
            )
            self._session.open()
        if self._session is None:
            raise RefillError("SPF V3 session is absent at a noninitial refill")
        signal, wire, timing = self._session.capture()
        native = _native_ci16_components(signal, wire.samples_per_channel)
        metadata = normalize_spf_v3_metadata(
            wire,
            timing,
            refill_index=refill_index,
            segment_sample_offset=segment_sample_offset,
        )
        return native, metadata

    def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            session.close()


def normalize_spf_v3_metadata(
    wire: SpfV3Metadata,
    timing: Mapping[str, object],
    *,
    refill_index: int,
    segment_sample_offset: int,
) -> RefillMetadata:
    """Validate one parsed V3 record and remove all wire-specific types."""

    flags = int(wire.flags)
    missing = _REQUIRED_FLAGS & ~flags
    if missing:
        raise RefillError(
            f"SPF V3 metadata lacks required validity flags: 0x{missing:x}"
        )
    if wire.channel_count != 2 or wire.enabled_scan_mask != 0x0F:
        raise RefillError("SPF V3 metadata does not describe paired RX1/RX2")
    if wire.iq_payload_bytes != wire.samples_per_channel * 8:
        raise RefillError("SPF V3 IQ byte count differs from paired CI16")
    if timing.get("sample_time_valid") is not True:
        raise RefillError("SPF V3 sample time is not valid")
    try:
        times = {key: _exact_int(timing[key], key) for key in _TIMING_KEYS}
    except KeyError as error:
        raise RefillError(f"SPF V3 timing lacks {error.args[0]}") from error

    observations = tuple(
        _normalize_observation(item) for item in wire.gain_observations
    )
    return RefillMetadata(
        refill_index=refill_index,
        segment_sample_offset=segment_sample_offset,
        sample_count=int(wire.samples_per_channel),
        stream_id=int(wire.stream_id),
        buffer_sequence=int(wire.buffer_sequence),
        first_sample_sequence=int(wire.first_sample_sequence),
        monotonic_start_ns=times["sample_time_monotonic_start_ns"],
        monotonic_end_ns=times["sample_time_monotonic_end_ns"],
        utc_start_ns=times["sample_time_realtime_start_ns"],
        utc_end_ns=times["sample_time_realtime_end_ns"],
        time_uncertainty_ns=times["sample_time_uncertainty_ns"],
        gain_db_start=(float(wire.rx1_gain_db_start), float(wire.rx2_gain_db_start)),
        gain_db_end=(float(wire.rx1_gain_db_end), float(wire.rx2_gain_db_end)),
        rssi_db_start=(wire.rx1_rssi_start_qdb / 4.0, wire.rx2_rssi_start_qdb / 4.0),
        rssi_db_end=(wire.rx1_rssi_end_qdb / 4.0, wire.rx2_rssi_end_qdb / 4.0),
        gain_observation_overflow_count=int(wire.gain_observation_overflow_count),
        gain_event_overflow_count=int(wire.gain_event_overflow_count),
        gain_observations=observations,
        flags=_failure_flags(flags),
    )


def spf_iio_session_factory(
    device: PlutoDevice, *, sample_rate_hz: int, samples_per_channel: int
) -> SpfV3CaptureSession:
    """Lazy optional composition hook for the pinned SPF host extension."""

    try:
        module = importlib.import_module("spf.direct_radio.iio_metadata")
        session_type = module.IioMetadataRx
    except (ImportError, AttributeError) as error:
        raise RuntimeError(
            "verified V5 capture requires the pinned SPF libiio host extension"
        ) from error
    return cast(
        SpfV3CaptureSession,
        session_type(
            device,
            sample_rate_hz=sample_rate_hz,
            samples_per_channel=samples_per_channel,
        ),
    )


def _normalize_observation(item: SpfV3GainObservation) -> GainObservation:
    required = _OBSERVATION_VALID | _OBSERVATION_INTERVAL_VALID
    if int(item.flags) & required != required:
        raise RefillError("SPF V3 gain observation lacks a valid sample interval")
    return GainObservation(
        sample_sequence_before=int(item.sample_sequence_before),
        sample_sequence_after=int(item.sample_sequence_after),
        read_duration_ns=int(item.read_duration_ns),
        gain_db=(float(item.rx1_gain_db), float(item.rx2_gain_db)),
    )


def _failure_flags(flags: int) -> tuple[RefillFlag, ...]:
    mapping = (
        (_DEVICE_IIO_OVERFLOW, RefillFlag.DEVICE_IIO_OVERFLOW),
        (_GAIN_READ_FAILED, RefillFlag.GAIN_READ_FAILED),
        (_RSSI_READ_FAILED, RefillFlag.RSSI_READ_FAILED),
        (_GAIN_OBSERVATION_OVERFLOW, RefillFlag.GAIN_OBSERVATION_OVERFLOW),
        (_FPGA_EVENT_OVERFLOW, RefillFlag.FPGA_EVENT_OVERFLOW),
    )
    return tuple(value for mask, value in mapping if flags & mask)


def _native_ci16_components(signal: object, sample_count: int) -> NativeComponents:
    try:
        numpy = importlib.import_module("numpy")
    except ImportError as error:
        raise RuntimeError("V5 hardware capture requires numpy") from error
    matrix = numpy.asarray(signal)
    if matrix.shape != (2, sample_count) or not numpy.iscomplexobj(matrix):
        raise RefillError("SPF V3 IQ is not a two-channel complex matrix")
    real = numpy.real(matrix)
    imag = numpy.imag(matrix)
    if (
        not numpy.all(numpy.isfinite(real))
        or not numpy.all(numpy.isfinite(imag))
        or not numpy.all(real == numpy.rint(real))
        or not numpy.all(imag == numpy.rint(imag))
        or numpy.any(real < -32768)
        or numpy.any(real > 32767)
        or numpy.any(imag < -32768)
        or numpy.any(imag > 32767)
    ):
        raise RefillError("SPF V3 IQ cannot be represented exactly as CI16")
    return (
        real[0].astype("int16"),
        imag[0].astype("int16"),
        real[1].astype("int16"),
        imag[1].astype("int16"),
    )


def _exact_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RefillError(f"SPF V3 {name} is not an integer")
    return value
