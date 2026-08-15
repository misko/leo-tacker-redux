"""Fail-closed, finite TX2 adapter for a conducted V5 bench fixture.

This module is deliberately not imported by capture.  It owns only the
operator-armed transmission side of a conducted validation fixture.
"""

from __future__ import annotations

import hashlib
import importlib
import math
import struct
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from leo_flow.capture.drivers.v5_observers import (
    observe_current_v5_runtime,
    observe_v5_radio,
)
from leo_flow.capture.drivers.v5_preflight import (
    ExpectedV5Radio,
    ExpectedV5Runtime,
    RadioObserver,
    RuntimeObserver,
    attest_v5,
)

TX2_DDS_CHANNEL_IDS = (
    "altvoltage4",
    "altvoltage5",
    "altvoltage6",
    "altvoltage7",
)
CONDUCTED_TOPOLOGY = "TX2->ATTENUATORS->PASSIVE_SPLITTER->RX1+RX2"
CONDUCTED_CONFIRMATION = "TX2_CONDUCTED_RX1_RX2_NO_ANTENNA"
TX_AUTHORIZATION = "AUTHORIZE_ONE_FINITE_CONDUCTED_TX2_SEND"
MUTED_TX2_GAIN_DB = -80.0
ALLOWED_ATTENUATION_DB = (80, 70, 60, 50, 40)
ALLOWED_WAVEFORM_RMS_COUNTS = (16, 32, 64, 128)
MINIMUM_PATH_ATTENUATION_DB = 30.0
MAXIMUM_STEPS = 8
MAXIMUM_SAMPLE_COUNT = 262_144
MAXIMUM_COMPONENT_PEAK = 512
MINIMUM_SAMPLE_RATE_HZ = 1_000_000
MAXIMUM_SAMPLE_RATE_HZ = 5_000_000
MINIMUM_LO_HZ = 325_000_000
MAXIMUM_LO_HZ = 3_800_000_000
FREQUENCY_READBACK_TOLERANCE_HZ = 2.0
IO_TIMEOUT_MS = 5_000
MAXIMUM_AUTHORIZATION_WINDOW_NS = 15 * 60 * 1_000_000_000
MAXIMUM_ATTENUATION_EVIDENCE_AGE_NS = 4 * 60 * 60 * 1_000_000_000


class Tx2SafetyError(RuntimeError):
    """A conducted-transmit safety gate failed closed."""


class Tx2CleanupError(Tx2SafetyError):
    """One or more mandatory mute/close operations failed."""


class Tx2Device(Protocol):
    """Narrow mutable port used only by the conducted TX fixture."""

    def read_serial(self) -> str: ...

    def attest_qualified_v5(
        self,
        uri: str,
        expected_runtime: ExpectedV5Runtime,
        expected_radio: ExpectedV5Radio,
    ) -> None: ...

    def destroy_tx_buffer(self) -> None: ...

    def disable_tx2_dds(self) -> None: ...

    def read_tx2_dds_scales(self) -> Mapping[str, float]: ...

    def set_tx2_gain_db(self, value: float) -> None: ...

    def read_tx2_gain_db(self) -> float: ...

    def set_tx2_lo_hz(self, value: int) -> None: ...

    def read_tx2_lo_hz(self) -> float: ...

    def set_sample_rate_hz(self, value: int) -> None: ...

    def read_sample_rate_hz(self) -> float: ...

    def transmit_tx2_finite_ci16(self, value: bytes) -> None: ...

    def close(self) -> None: ...


Tx2DeviceFactory = Callable[[str], Tx2Device]


@dataclass(frozen=True, slots=True)
class ConductedPathAttenuationEvidence:
    """Independent, digest-bound attenuation evidence for one energized path."""

    receiver_path: Literal["RX1", "RX2"]
    attenuator_ids: tuple[str, ...]
    attenuation_db: float
    verified_by: str
    verification_method: Literal[
        "calibrated_vna", "calibrated_signal_generator_power_meter"
    ]
    verified_utc_ns: int
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class ConductedFixtureAttestation:
    """Independent evidence for the physical, antenna-free bench topology."""

    radio_serial: str
    topology: str
    splitter_id: str
    path_evidence: tuple[
        ConductedPathAttenuationEvidence, ConductedPathAttenuationEvidence
    ]
    confirmation: str


@dataclass(frozen=True, slots=True)
class FiniteTx2Waveform:
    ci16_le: bytes
    declared_rms_counts: int
    sha256: str

    @classmethod
    def from_ci16(
        cls, ci16_le: bytes, *, declared_rms_counts: int
    ) -> FiniteTx2Waveform:
        return cls(
            bytes(ci16_le),
            declared_rms_counts,
            hashlib.sha256(ci16_le).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class Tx2LadderStep:
    tx_attenuation_db: int
    waveform: FiniteTx2Waveform


@dataclass(frozen=True, slots=True)
class ConductedTx2Plan:
    """One exact, finite, operator-armed TX2 operation."""

    uri: str
    expected_radio_serial: str
    armed_radio_serial: str
    topology: ConductedFixtureAttestation
    expected_runtime: ExpectedV5Runtime
    expected_radio: ExpectedV5Radio
    tx_lo_hz: int
    sample_rate_hz: int
    steps: tuple[Tx2LadderStep, ...]
    tx_operator_id: str = ""
    tx_authorization: str = ""
    authorization_issued_utc_ns: int = 0
    authorization_expires_utc_ns: int = 0


@dataclass(frozen=True, slots=True)
class Tx2StepEvidence:
    index: int
    tx_attenuation_db: int
    tx_gain_readback_db: float
    waveform_rms_counts: int
    waveform_sha256: str
    sample_count: int
    tx_lo_readback_hz: float
    sample_rate_readback_hz: float
    pre_mute: Tx2MuteEvidence
    post_mute: Tx2MuteEvidence


@dataclass(frozen=True, slots=True)
class Tx2MuteEvidence:
    tx_buffer_destroyed: bool
    tx_gain_readback_db: float
    tx2_dds_scale_readbacks: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class ConductedTx2Evidence:
    radio_serial: str
    uri: str
    topology: str
    splitter_id: str
    steps: tuple[Tx2StepEvidence, ...]
    initial_mute: Tx2MuteEvidence
    final_mute: Tx2MuteEvidence
    final_state_verified_muted: bool


@dataclass(frozen=True, slots=True)
class ConductedTx2PreflightEvidence:
    """Read-only identity and V5 attestation evidence for one exact context."""

    radio_serial: str
    uri: str
    context_closed: bool


def validate_conducted_tx2_plan(plan: ConductedTx2Plan) -> None:
    """Validate every immutable safety gate without opening a radio context."""

    _validate_plan(plan)


def preflight_conducted_tx2(
    plan: ConductedTx2Plan,
    device_factory: Tx2DeviceFactory,
) -> ConductedTx2PreflightEvidence:
    """Attest an exact V5 context without changing any radio attribute."""

    _validate_plan(plan)
    device: Tx2Device | None = None
    primary_error: Exception | None = None
    observed_serial: str | None = None
    try:
        device = device_factory(plan.uri)
        device.attest_qualified_v5(
            plan.uri,
            plan.expected_runtime,
            plan.expected_radio,
        )
        observed_serial = device.read_serial()
        if observed_serial != plan.expected_radio_serial:
            raise Tx2SafetyError(
                "selected radio serial does not match the exact armed V5 serial"
            )
    except Exception as error:  # noqa: BLE001 - exact context must always close
        primary_error = error
    finally:
        if device is not None:
            try:
                device.close()
            except Exception:  # noqa: BLE001 - surface close failure
                raise Tx2CleanupError(
                    "mandatory read-only preflight context close failed"
                ) from primary_error
    if primary_error is not None:
        raise primary_error
    if observed_serial is None:  # pragma: no cover - defensive invariant
        raise Tx2SafetyError("read-only preflight produced no radio identity")
    return ConductedTx2PreflightEvidence(
        radio_serial=observed_serial,
        uri=plan.uri,
        context_closed=True,
    )


def run_conducted_tx2_ladder(
    plan: ConductedTx2Plan,
    device_factory: Tx2DeviceFactory,
    *,
    now_utc_ns: int | None = None,
) -> ConductedTx2Evidence:
    """Run a bounded finite ladder and always return the verified radio to mute.

    The complete plan and every waveform are validated before a context is
    opened.  The factory receives the exact URI; this module has no discovery
    path or implicit default device.
    """

    _validate_plan(plan)
    _validate_execution_window(
        plan, time.time_ns() if now_utc_ns is None else now_utc_ns
    )
    device: Tx2Device | None = None
    identity_verified = False
    primary_error: Exception | None = None
    evidence: list[Tx2StepEvidence] = []
    initial_mute: Tx2MuteEvidence | None = None
    final_mute: Tx2MuteEvidence | None = None
    try:
        device = device_factory(plan.uri)
        device.attest_qualified_v5(
            plan.uri,
            plan.expected_runtime,
            plan.expected_radio,
        )
        observed_serial = device.read_serial()
        if observed_serial != plan.expected_radio_serial:
            raise Tx2SafetyError(
                "selected radio serial does not match the exact armed V5 serial"
            )
        identity_verified = True
        initial_mute = _mute_and_verify(device)
        device.set_tx2_lo_hz(plan.tx_lo_hz)
        _require_readback(
            "TX2 LO",
            device.read_tx2_lo_hz(),
            plan.tx_lo_hz,
            FREQUENCY_READBACK_TOLERANCE_HZ,
        )
        device.set_sample_rate_hz(plan.sample_rate_hz)
        _require_readback(
            "sample rate",
            device.read_sample_rate_hz(),
            plan.sample_rate_hz,
            FREQUENCY_READBACK_TOLERANCE_HZ,
        )
        for index, step in enumerate(plan.steps):
            pre_mute = _mute_and_verify(device)
            lo_readback = device.read_tx2_lo_hz()
            rate_readback = device.read_sample_rate_hz()
            _require_readback(
                "TX2 LO",
                lo_readback,
                plan.tx_lo_hz,
                FREQUENCY_READBACK_TOLERANCE_HZ,
            )
            _require_readback(
                "sample rate",
                rate_readback,
                plan.sample_rate_hz,
                FREQUENCY_READBACK_TOLERANCE_HZ,
            )
            requested_gain = -float(step.tx_attenuation_db)
            post_mute: Tx2MuteEvidence | None = None
            try:
                device.set_tx2_gain_db(requested_gain)
                gain_readback = device.read_tx2_gain_db()
                _require_readback("TX2 gain", gain_readback, requested_gain, 0.01)
                _require_dds_muted(device)
                device.transmit_tx2_finite_ci16(step.waveform.ci16_le)
            finally:
                post_mute = _mute_and_verify(device)
            assert post_mute is not None
            evidence.append(
                Tx2StepEvidence(
                    index=index,
                    tx_attenuation_db=step.tx_attenuation_db,
                    tx_gain_readback_db=gain_readback,
                    waveform_rms_counts=step.waveform.declared_rms_counts,
                    waveform_sha256=step.waveform.sha256,
                    sample_count=len(step.waveform.ci16_le) // 4,
                    tx_lo_readback_hz=lo_readback,
                    sample_rate_readback_hz=rate_readback,
                    pre_mute=pre_mute,
                    post_mute=post_mute,
                )
            )
        final_mute = _mute_and_verify(device)
    except Exception as error:  # noqa: BLE001 - cleanup must follow every failure
        primary_error = error
    finally:
        cleanup_errors = _cleanup(device, identity_verified=identity_verified)
        if cleanup_errors:
            detail = "; ".join(
                f"{operation}: {type(error).__name__}"
                for operation, error in cleanup_errors
            )
            raise Tx2CleanupError(
                f"mandatory TX2 cleanup failed: {detail}"
            ) from primary_error
    if primary_error is not None:
        raise primary_error
    if initial_mute is None or final_mute is None:  # pragma: no cover - invariant
        raise Tx2SafetyError("successful TX2 run lacks mute evidence")
    return ConductedTx2Evidence(
        radio_serial=plan.expected_radio_serial,
        uri=plan.uri,
        topology=plan.topology.topology,
        splitter_id=plan.topology.splitter_id,
        steps=tuple(evidence),
        initial_mute=initial_mute,
        final_mute=final_mute,
        final_state_verified_muted=True,
    )


def _validate_plan(plan: ConductedTx2Plan) -> None:
    if not (
        (plan.uri.startswith("ip:") and len(plan.uri) > 3)
        or (plan.uri.startswith("usb:") and len(plan.uri) > 4)
    ) or any(character.isspace() for character in plan.uri):
        raise Tx2SafetyError("an exact standard-libiio ip: or usb: URI is required")
    if not plan.expected_radio_serial or (
        plan.armed_radio_serial != plan.expected_radio_serial
    ):
        raise Tx2SafetyError("the exact expected V5 serial was not explicitly armed")
    if not plan.tx_operator_id.strip():
        raise Tx2SafetyError("an identified TX operator is required")
    if plan.tx_authorization != TX_AUTHORIZATION:
        raise Tx2SafetyError("the exact finite TX authorization was not provided")
    if (
        isinstance(plan.authorization_issued_utc_ns, bool)
        or not isinstance(plan.authorization_issued_utc_ns, int)
        or isinstance(plan.authorization_expires_utc_ns, bool)
        or not isinstance(plan.authorization_expires_utc_ns, int)
        or plan.authorization_issued_utc_ns <= 0
        or plan.authorization_expires_utc_ns <= plan.authorization_issued_utc_ns
        or plan.authorization_expires_utc_ns - plan.authorization_issued_utc_ns
        > MAXIMUM_AUTHORIZATION_WINDOW_NS
    ):
        raise Tx2SafetyError(
            "TX authorization needs a positive bounded issue/expiry window"
        )
    if plan.expected_radio.serial != plan.expected_radio_serial:
        raise Tx2SafetyError("the V5 radio attestation names a different serial")
    if (
        plan.expected_radio.enabled_scan_mask != 0x0F
        or plan.expected_radio.channel_count != 2
        or plan.expected_radio.component_layout != ("I0", "Q0", "I1", "Q1")
        or plan.expected_radio.maximum_tx2_hardware_gain_db != MUTED_TX2_GAIN_DB
        or plan.expected_radio.tx2_dds_channel_ids != TX2_DDS_CHANNEL_IDS
    ):
        raise Tx2SafetyError(
            "the expected radio is not the qualified V5 2RX/2TX layout"
        )
    _validate_topology(
        plan.topology,
        plan.expected_radio_serial,
        tx_operator_id=plan.tx_operator_id,
    )
    if not MINIMUM_LO_HZ <= plan.tx_lo_hz <= MAXIMUM_LO_HZ:
        raise Tx2SafetyError("TX2 LO lies outside the bounded conducted-fixture range")
    if not MINIMUM_SAMPLE_RATE_HZ <= plan.sample_rate_hz <= MAXIMUM_SAMPLE_RATE_HZ:
        raise Tx2SafetyError("sample rate lies outside the bounded fixture range")
    if not 1 <= len(plan.steps) <= MAXIMUM_STEPS:
        raise Tx2SafetyError("the finite TX2 ladder has an invalid number of steps")

    prior_position: tuple[int, int] | None = None
    for index, step in enumerate(plan.steps):
        try:
            attenuation_index = ALLOWED_ATTENUATION_DB.index(step.tx_attenuation_db)
            level_index = ALLOWED_WAVEFORM_RMS_COUNTS.index(
                step.waveform.declared_rms_counts
            )
        except ValueError as error:
            raise Tx2SafetyError(
                "TX2 step is outside the fixed safety ladder"
            ) from error
        position = (attenuation_index, level_index)
        if index == 0 and position != (0, 0):
            raise Tx2SafetyError(
                "the ladder must start at 80 dB attenuation and 16 RMS counts"
            )
        if prior_position is not None:
            delta = tuple(
                current - prior for current, prior in zip(position, prior_position)
            )
            if delta not in ((1, 0), (0, 1)):
                raise Tx2SafetyError(
                    "each ladder step must raise exactly one bounded risk dimension"
                )
        _validate_waveform(step.waveform)
        prior_position = position


def _validate_topology(
    topology: ConductedFixtureAttestation,
    expected_serial: str,
    *,
    tx_operator_id: str,
) -> None:
    if topology.radio_serial != expected_serial:
        raise Tx2SafetyError("topology attestation names a different radio serial")
    if topology.topology != CONDUCTED_TOPOLOGY:
        raise Tx2SafetyError(
            "the exact conducted TX2-to-RX1/RX2 topology was not attested"
        )
    if topology.confirmation != CONDUCTED_CONFIRMATION:
        raise Tx2SafetyError("the antenna-free conducted topology was not confirmed")
    if not topology.splitter_id.strip():
        raise Tx2SafetyError("the passive splitter/tee identity is required")
    if tuple(item.receiver_path for item in topology.path_evidence) != ("RX1", "RX2"):
        raise Tx2SafetyError(
            "attenuation evidence must cover energized RX1 and RX2 paths in order"
        )
    all_attenuator_ids: list[str] = []
    for item in topology.path_evidence:
        if not item.attenuator_ids or any(
            not value.strip() for value in item.attenuator_ids
        ):
            raise Tx2SafetyError("each receiver path requires identified attenuators")
        if (
            not math.isfinite(item.attenuation_db)
            or item.attenuation_db < MINIMUM_PATH_ATTENUATION_DB
        ):
            raise Tx2SafetyError(
                "each receiver path requires at least 30 dB attenuation"
            )
        if not item.verified_by.strip() or (
            item.verified_by.strip().casefold() == tx_operator_id.strip().casefold()
        ):
            raise Tx2SafetyError(
                "each receiver path needs an independent identified verifier"
            )
        if item.verification_method not in (
            "calibrated_vna",
            "calibrated_signal_generator_power_meter",
        ):
            raise Tx2SafetyError(
                "attenuation verification method is not independently calibrated"
            )
        if (
            isinstance(item.verified_utc_ns, bool)
            or not isinstance(item.verified_utc_ns, int)
            or item.verified_utc_ns <= 0
        ):
            raise Tx2SafetyError("attenuation verification time is required")
        if len(item.evidence_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in item.evidence_sha256
        ):
            raise Tx2SafetyError(
                "attenuation verification evidence needs a lowercase SHA-256"
            )
        all_attenuator_ids.extend(item.attenuator_ids)
    if len(set(all_attenuator_ids)) != len(all_attenuator_ids):
        raise Tx2SafetyError(
            "energized receiver paths require distinct attenuator identities"
        )


def _validate_execution_window(plan: ConductedTx2Plan, now_utc_ns: int) -> None:
    if (
        isinstance(now_utc_ns, bool)
        or not isinstance(now_utc_ns, int)
        or now_utc_ns <= 0
    ):
        raise Tx2SafetyError("execution time must be a positive UTC nanosecond value")
    if plan.authorization_issued_utc_ns > now_utc_ns:
        raise Tx2SafetyError("TX authorization is from the future")
    if plan.authorization_expires_utc_ns < now_utc_ns:
        raise Tx2SafetyError("TX authorization is stale")
    for item in plan.topology.path_evidence:
        if item.verified_utc_ns > now_utc_ns:
            raise Tx2SafetyError("attenuation evidence is from the future")
        if now_utc_ns - item.verified_utc_ns > MAXIMUM_ATTENUATION_EVIDENCE_AGE_NS:
            raise Tx2SafetyError("attenuation evidence is stale")


def _validate_waveform(waveform: FiniteTx2Waveform) -> None:
    if not waveform.ci16_le or len(waveform.ci16_le) % 4:
        raise Tx2SafetyError("waveform must be a nonempty interleaved CI16 stream")
    sample_count = len(waveform.ci16_le) // 4
    if sample_count > MAXIMUM_SAMPLE_COUNT:
        raise Tx2SafetyError("waveform exceeds the finite sample-count bound")
    if hashlib.sha256(waveform.ci16_le).hexdigest() != waveform.sha256:
        raise Tx2SafetyError("waveform digest does not match its armed bytes")
    sum_power = 0
    peak = 0
    for i_value, q_value in struct.iter_unpack("<hh", waveform.ci16_le):
        peak = max(peak, abs(i_value), abs(q_value))
        sum_power += i_value * i_value + q_value * q_value
    observed_rms = math.sqrt(sum_power / sample_count)
    tolerance = max(1.0, waveform.declared_rms_counts * 0.03)
    if abs(observed_rms - waveform.declared_rms_counts) > tolerance:
        raise Tx2SafetyError("waveform RMS does not match its bounded declared level")
    if peak > MAXIMUM_COMPONENT_PEAK:
        raise Tx2SafetyError("waveform component peak exceeds the hard fixture bound")


def _mute_and_verify(device: Tx2Device) -> Tx2MuteEvidence:
    device.destroy_tx_buffer()
    device.disable_tx2_dds()
    device.set_tx2_gain_db(MUTED_TX2_GAIN_DB)
    gain_readback = device.read_tx2_gain_db()
    _require_readback(
        "muted TX2 gain",
        gain_readback,
        MUTED_TX2_GAIN_DB,
        0.01,
    )
    scales = _require_dds_muted(device)
    return Tx2MuteEvidence(True, gain_readback, scales)


def _require_dds_muted(device: Tx2Device) -> tuple[tuple[str, float], ...]:
    scales = dict(device.read_tx2_dds_scales())
    if set(scales) != set(TX2_DDS_CHANNEL_IDS) or any(
        not math.isfinite(value) or value != 0.0 for value in scales.values()
    ):
        raise Tx2SafetyError("TX2 DDS mute readback is incomplete or nonzero")
    return tuple(sorted(scales.items()))


def _require_readback(
    label: str, observed: float, expected: float, tolerance: float
) -> None:
    if not math.isfinite(observed) or abs(observed - expected) > tolerance:
        raise Tx2SafetyError(f"{label} readback differs from the armed value")


def _cleanup(
    device: Tx2Device | None, *, identity_verified: bool
) -> list[tuple[str, Exception]]:
    if device is None:
        return []
    errors: list[tuple[str, Exception]] = []
    operations: list[tuple[str, Callable[[], object]]] = []
    if identity_verified:
        operations.extend(
            (
                ("destroy TX buffer", device.destroy_tx_buffer),
                ("disable TX2 DDS", device.disable_tx2_dds),
                (
                    "set TX2 muted gain",
                    lambda: device.set_tx2_gain_db(MUTED_TX2_GAIN_DB),
                ),
                ("verify TX2 muted gain", lambda: _verify_gain_muted(device)),
                ("verify TX2 DDS mute", lambda: _require_dds_muted(device)),
            )
        )
    operations.append(("close exact context", device.close))
    for name, operation in operations:
        try:
            operation()
        except Exception as error:  # noqa: BLE001 - attempt every cleanup action
            errors.append((name, error))
    return errors


def _verify_gain_muted(device: Tx2Device) -> None:
    _require_readback(
        "muted TX2 gain",
        device.read_tx2_gain_db(),
        MUTED_TX2_GAIN_DB,
        0.01,
    )


class PyadiTx2Device:
    """Lazy pyadi implementation for one exact standard-libiio context."""

    def __init__(
        self,
        uri: str,
        *,
        module_loader: Callable[[str], Any] = importlib.import_module,
        runtime_observer: RuntimeObserver = observe_current_v5_runtime,
        radio_observer: RadioObserver = observe_v5_radio,
    ) -> None:
        if not uri or uri in {"ip:", "usb:"}:
            raise Tx2SafetyError("Pyadi TX2 requires an exact context URI")
        adi = module_loader("adi")
        self._numpy = module_loader("numpy")
        # ad9361 is required for the qualified V5 two-RX/two-TX layout.  The
        # exact URI is always passed; pyadi discovery is never invoked.
        self._runtime_observer = runtime_observer
        self._radio_observer = radio_observer
        self._device: Any | None = adi.ad9361(uri=uri)
        context = getattr(self._device, "_ctx", None)
        if context is None:
            try:
                self._close_unverified_device()
            except Exception as error:
                raise Tx2CleanupError(
                    "pyadi device lacked a context and close failed"
                ) from error
            raise Tx2SafetyError("selected pyadi device exposes no libiio context")
        set_timeout = getattr(context, "set_timeout", None)
        if not callable(set_timeout):
            self._close_unverified_device()
            raise Tx2SafetyError("selected context cannot install a finite I/O timeout")
        try:
            set_timeout(IO_TIMEOUT_MS)
        except Exception as primary_error:
            try:
                self._close_unverified_device()
            except Exception as cleanup_error:
                raise Tx2CleanupError(
                    "I/O timeout setup and unverified-device close both failed"
                ) from cleanup_error
            raise Tx2SafetyError("finite I/O timeout setup failed") from primary_error
        self._context: Any = context

    def attest_qualified_v5(
        self,
        uri: str,
        expected_runtime: ExpectedV5Runtime,
        expected_radio: ExpectedV5Radio,
    ) -> None:
        """Apply the existing V5 host/radio gate before any TX mutation."""

        try:
            observed_runtime = self._runtime_observer()
            observed_radio = self._radio_observer(self._live_device())
            attest_v5(
                uri=uri,
                expected_runtime=expected_runtime,
                observed_runtime=observed_runtime,
                expected_radio=expected_radio,
                observed_radio=observed_radio,
            )
        except Exception as error:
            raise Tx2SafetyError(
                f"qualified V5 host/radio attestation failed: {type(error).__name__}"
            ) from error

    def read_serial(self) -> str:
        direct = getattr(self._live_device(), "serial", None)
        if direct:
            return str(direct)
        attrs = getattr(self._context, "attrs", {})
        for key in ("hw_serial", "serial"):
            if key in attrs:
                return str(getattr(attrs[key], "value", attrs[key]))
        raise Tx2SafetyError("selected context exposes no radio serial")

    def destroy_tx_buffer(self) -> None:
        destroy = getattr(self._live_device(), "tx_destroy_buffer", None)
        if callable(destroy):
            destroy()

    def disable_tx2_dds(self) -> None:
        for channel in self._tx2_dds_channels().values():
            channel.attrs["scale"].value = "0"

    def read_tx2_dds_scales(self) -> Mapping[str, float]:
        return {
            name: float(str(channel.attrs["scale"].value).split()[0])
            for name, channel in self._tx2_dds_channels().items()
        }

    def set_tx2_gain_db(self, value: float) -> None:
        self._live_device().tx_hardwaregain_chan1 = value

    def read_tx2_gain_db(self) -> float:
        return float(self._live_device().tx_hardwaregain_chan1)

    def set_tx2_lo_hz(self, value: int) -> None:
        self._live_device().tx_lo = value

    def read_tx2_lo_hz(self) -> float:
        return float(self._live_device().tx_lo)

    def set_sample_rate_hz(self, value: int) -> None:
        self._live_device().sample_rate = value

    def read_sample_rate_hz(self) -> float:
        return float(self._live_device().sample_rate)

    def transmit_tx2_finite_ci16(self, value: bytes) -> None:
        components = self._numpy.frombuffer(value, dtype="<i2").reshape((-1, 2))
        samples = components[:, 0].astype(self._numpy.float32) + (
            1j * components[:, 1].astype(self._numpy.float32)
        )
        device = self._live_device()
        device.tx_enabled_channels = [1]
        device.tx_cyclic_buffer = False
        if bool(device.tx_cyclic_buffer):
            raise Tx2SafetyError("pyadi TX buffer did not accept finite mode")
        device.tx(samples)

    def close(self) -> None:
        # pyadi 0.0.21 close() clears only device._ctx. Its cached IIO devices
        # and this adapter's context alias also retain the pylibiio Context,
        # whose native close is reference-count driven. Drop every owned
        # reference before returning, including when pyadi close itself fails.
        device, self._device = self._device, None
        context, self._context = self._context, None
        if device is None:
            return
        close = getattr(device, "close", None)
        try:
            if callable(close):
                close()
            else:
                destroy = getattr(context, "destroy", None)
                if callable(destroy):
                    destroy()
        finally:
            del device
            del context

    def _tx2_dds_channels(self) -> dict[str, Any]:
        dds = self._context.find_device("cf-ad9361-dds-core-lpc")
        if dds is None:
            raise Tx2SafetyError("selected context lacks the TX DDS core")
        channels = {
            str(channel.id): channel
            for channel in dds.channels
            if bool(getattr(channel, "output", False))
            and str(getattr(channel, "id", "")) in TX2_DDS_CHANNEL_IDS
        }
        if set(channels) != set(TX2_DDS_CHANNEL_IDS):
            raise Tx2SafetyError("selected context lacks the complete TX2 DDS layout")
        return channels

    def _close_unverified_device(self) -> None:
        device, self._device = self._device, None
        if device is None:
            return
        context = getattr(device, "_ctx", None)
        close = getattr(device, "close", None)
        try:
            if callable(close):
                close()
            else:
                destroy = getattr(context, "destroy", None)
                if callable(destroy):
                    destroy()
        finally:
            del device
            del context

    def _live_device(self) -> Any:
        if self._device is None:
            raise Tx2SafetyError("pyadi TX2 device is closed")
        return self._device


def open_exact_pyadi_tx2(uri: str) -> PyadiTx2Device:
    """Open only the exact URI supplied by an already validated plan."""

    return PyadiTx2Device(uri)
