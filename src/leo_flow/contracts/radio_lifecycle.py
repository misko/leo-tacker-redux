"""Public v0.1 contracts for bounded radio lifecycle observations and facts.

These contracts deliberately carry structured identity evidence, not command
output or logs.  Capture and dashboard components can therefore consume reboot
facts without depending on SSH, libiio, a firmware implementation, or storage.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ._validation import require_token, require_utc_ns
from .core import (
    V0_1,
    CaptureAttemptId,
    CaptureBatchId,
    RadioId,
    SchemaRef,
    UtcNs,
)

_RESET_REASON = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}")
_EVIDENCE_CODES = frozenset(
    {
        "ad9361_initialization_epoch_changed",
        "boot_id_changed",
        "boot_id_unchanged",
        "iiod_process_identity_changed",
        "iiod_process_identity_unchanged",
        "lifecycle_identity_unchanged",
        "lifecycle_observer_unavailable",
        "transport_failed",
    }
)


def _nonnegative_int(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")


class RadioLifecycleObservationStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class RadioLifecycleObservationSource(str, Enum):
    """Bounded adapters allowed to provide a lifecycle observation."""

    AUTHENTICATED_DIAGNOSTIC_V1 = "authenticated_diagnostic_v1"
    CAPTURE_METADATA_V4 = "capture_metadata_v4"
    AUTHENTICATED_HOST_FALLBACK_V1 = "authenticated_host_fallback_v1"


class RadioLifecycleTrust(str, Enum):
    RADIO_AUTHENTICATED = "radio_authenticated"
    FIRMWARE_METADATA_AUTHENTICATED = "firmware_metadata_authenticated"
    HOST_AUTHENTICATED = "host_authenticated"


class RadioLifecycleObserverUnavailableReason(str, Enum):
    DEADLINE_EXCEEDED = "deadline_exceeded"
    AUTHENTICATION_FAILED = "authentication_failed"
    INVALID_RESPONSE = "invalid_response"
    UNSUPPORTED = "unsupported"
    OBSERVER_ERROR = "observer_error"


@dataclass(frozen=True)
class IiodProcessIdentityV0_1:
    """Linux process identity; PID alone is intentionally insufficient."""

    pid: int
    proc_start_ticks: int
    clock_ticks_per_second: int

    def __post_init__(self) -> None:
        _nonnegative_int(self.pid, "pid")
        _nonnegative_int(self.proc_start_ticks, "proc_start_ticks")
        if (
            isinstance(self.clock_ticks_per_second, bool)
            or not isinstance(self.clock_ticks_per_second, int)
            or self.clock_ticks_per_second <= 0
        ):
            raise ValueError("clock_ticks_per_second must be a positive integer")


@dataclass(frozen=True)
class Ad9361LifecycleIdentityV0_1:
    """Optional firmware-maintained AD9361 initialization generation."""

    initialization_epoch: int
    reset_reason: str | None = None

    def __post_init__(self) -> None:
        _nonnegative_int(self.initialization_epoch, "initialization_epoch")
        if (
            self.reset_reason is not None
            and _RESET_REASON.fullmatch(self.reset_reason) is None
        ):
            raise ValueError("reset_reason must be a bounded reason code")


@dataclass(frozen=True)
class RadioLifecycleObservationV0_1:
    """One bounded observation of the radio OS and receive-service lifecycle.

    ``estimated_boot_utc_ns`` is recorded for operators but is never an identity
    key: wall-clock correction may move it.  ``boot_id`` is the boot authority.
    """

    schema: SchemaRef
    radio_id: RadioId
    observed_utc_ns: UtcNs
    status: RadioLifecycleObservationStatus
    source: RadioLifecycleObservationSource
    trust: RadioLifecycleTrust
    boot_id: str | None
    uptime_ns: int | None
    estimated_boot_utc_ns: UtcNs | None
    boot_time_uncertainty_ns: int | None
    iiod: IiodProcessIdentityV0_1 | None
    ad9361: Ad9361LifecycleIdentityV0_1 | None = None
    unavailable_reason: RadioLifecycleObserverUnavailableReason | None = None

    SCHEMA_ID = "org.leo-flow.radio-lifecycle-observation"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported radio lifecycle observation schema")
        require_utc_ns(self.observed_utc_ns, "observed_utc_ns")
        if not isinstance(self.status, RadioLifecycleObservationStatus):
            raise TypeError("status must be a RadioLifecycleObservationStatus")
        if not isinstance(self.source, RadioLifecycleObservationSource):
            raise TypeError("source must be a RadioLifecycleObservationSource")
        if not isinstance(self.trust, RadioLifecycleTrust):
            raise TypeError("trust must be a RadioLifecycleTrust")
        expected_trust = {
            RadioLifecycleObservationSource.AUTHENTICATED_DIAGNOSTIC_V1: (
                RadioLifecycleTrust.RADIO_AUTHENTICATED
            ),
            RadioLifecycleObservationSource.CAPTURE_METADATA_V4: (
                RadioLifecycleTrust.FIRMWARE_METADATA_AUTHENTICATED
            ),
            RadioLifecycleObservationSource.AUTHENTICATED_HOST_FALLBACK_V1: (
                RadioLifecycleTrust.HOST_AUTHENTICATED
            ),
        }[self.source]
        if self.trust is not expected_trust:
            raise ValueError("lifecycle source and trust authority do not match")
        identity = (
            self.boot_id,
            self.uptime_ns,
            self.estimated_boot_utc_ns,
            self.boot_time_uncertainty_ns,
            self.iiod,
        )
        if self.status is RadioLifecycleObservationStatus.AVAILABLE:
            if any(value is None for value in identity):
                raise ValueError("available observation requires complete OS identity")
            assert self.boot_id is not None
            try:
                parsed = uuid.UUID(self.boot_id)
            except (AttributeError, ValueError) as error:
                raise ValueError("boot_id must be a canonical UUID") from error
            if str(parsed) != self.boot_id:
                raise ValueError("boot_id must be a lower-case canonical UUID")
            assert self.uptime_ns is not None
            assert self.estimated_boot_utc_ns is not None
            assert self.boot_time_uncertainty_ns is not None
            _nonnegative_int(self.uptime_ns, "uptime_ns")
            require_utc_ns(self.estimated_boot_utc_ns, "estimated_boot_utc_ns")
            _nonnegative_int(self.boot_time_uncertainty_ns, "boot_time_uncertainty_ns")
            estimated = int(self.observed_utc_ns) - self.uptime_ns
            if (
                estimated < 0
                or abs(int(self.estimated_boot_utc_ns) - estimated)
                > self.boot_time_uncertainty_ns
            ):
                raise ValueError(
                    "estimated_boot_utc_ns contradicts observed UTC and uptime"
                )
            if self.unavailable_reason is not None:
                raise ValueError("available observation cannot be unavailable")
        else:
            if any(value is not None for value in (*identity, self.ad9361)):
                raise ValueError("unavailable observation cannot claim identity")
            if self.unavailable_reason is None:
                raise ValueError("unavailable observation requires a reason code")
            if not isinstance(
                self.unavailable_reason, RadioLifecycleObserverUnavailableReason
            ):
                raise TypeError(
                    "unavailable_reason must be a fixed observer reason code"
                )


class RadioLifecycleReason(str, Enum):
    RADIO_REBOOTED = "radio_rebooted"
    IIOD_RESTARTED = "iiod_restarted"
    AD9361_REINITIALIZED = "ad9361_reinitialized"
    TRANSPORT_TIMEOUT_UNKNOWN = "transport_timeout_unknown"


class RadioLifecycleConfidence(str, Enum):
    HIGH = "high"
    LOW = "low"


class RadioTransportOutcome(str, Enum):
    COMPLETE = "complete"
    DISCONNECTED = "disconnected"
    TIMEOUT = "timeout"
    OTHER_FAILURE = "other_failure"


@dataclass(frozen=True)
class RadioLifecycleDiagnosisV0_1:
    reason: RadioLifecycleReason | None
    confidence: RadioLifecycleConfidence | None
    evidence_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.reason is not None and not isinstance(
            self.reason, RadioLifecycleReason
        ):
            raise TypeError("reason must be a RadioLifecycleReason")
        if self.confidence is not None and not isinstance(
            self.confidence, RadioLifecycleConfidence
        ):
            raise TypeError("confidence must be a RadioLifecycleConfidence")
        if not isinstance(self.evidence_codes, tuple):
            raise TypeError("evidence_codes must be an immutable tuple")
        for code in self.evidence_codes:
            require_token(code, "evidence code")
            if code not in _EVIDENCE_CODES:
                raise ValueError("evidence code is not part of lifecycle v0.1")
        if tuple(sorted(set(self.evidence_codes))) != self.evidence_codes:
            raise ValueError("evidence_codes must be unique and sorted")
        if (self.reason is None) != (self.confidence is None):
            raise ValueError("reason and confidence must either both be set or absent")
        if self.reason is None and self.evidence_codes:
            raise ValueError("no diagnosis cannot carry evidence codes")


def derive_radio_lifecycle_diagnosis_v0_1(
    before: RadioLifecycleObservationV0_1,
    after: RadioLifecycleObservationV0_1,
    *,
    transport_outcome: RadioTransportOutcome,
) -> RadioLifecycleDiagnosisV0_1:
    """Derive the sole valid diagnosis for two lifecycle observations."""

    if before.radio_id != after.radio_id:
        raise ValueError("cannot compare lifecycle observations from different radios")
    available = (
        before.status is RadioLifecycleObservationStatus.AVAILABLE
        and after.status is RadioLifecycleObservationStatus.AVAILABLE
    )
    if available:
        if before.boot_id != after.boot_id:
            return _diagnosis(
                RadioLifecycleReason.RADIO_REBOOTED,
                RadioLifecycleConfidence.HIGH,
                "boot_id_changed",
            )
        if before.iiod != after.iiod:
            return _diagnosis(
                RadioLifecycleReason.IIOD_RESTARTED,
                RadioLifecycleConfidence.HIGH,
                "boot_id_unchanged",
                "iiod_process_identity_changed",
            )
        if (
            before.ad9361 is not None
            and after.ad9361 is not None
            and before.ad9361.initialization_epoch != after.ad9361.initialization_epoch
        ):
            return _diagnosis(
                RadioLifecycleReason.AD9361_REINITIALIZED,
                RadioLifecycleConfidence.HIGH,
                "ad9361_initialization_epoch_changed",
                "boot_id_unchanged",
                "iiod_process_identity_unchanged",
            )
    if transport_outcome in (
        RadioTransportOutcome.DISCONNECTED,
        RadioTransportOutcome.TIMEOUT,
    ):
        evidence = ["transport_failed"]
        if not available:
            evidence.append("lifecycle_observer_unavailable")
        else:
            evidence.append("lifecycle_identity_unchanged")
        return _diagnosis(
            RadioLifecycleReason.TRANSPORT_TIMEOUT_UNKNOWN,
            RadioLifecycleConfidence.LOW,
            *evidence,
        )
    return RadioLifecycleDiagnosisV0_1(None, None, ())


@dataclass(frozen=True)
class CaptureAttemptLifecycleFactV0_1:
    """Immutable lifecycle evidence for one terminal capture attempt."""

    schema: SchemaRef
    batch_id: CaptureBatchId
    attempt_id: CaptureAttemptId
    radio_id: RadioId
    preflight: RadioLifecycleObservationV0_1
    terminal: RadioLifecycleObservationV0_1
    transport_outcome: RadioTransportOutcome
    diagnosis: RadioLifecycleDiagnosisV0_1

    SCHEMA_ID = "org.leo-flow.capture-attempt-radio-lifecycle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported attempt lifecycle fact schema")
        if (
            self.preflight.radio_id != self.radio_id
            or self.terminal.radio_id != self.radio_id
        ):
            raise ValueError("lifecycle observations must belong to the attempt radio")
        if self.terminal.observed_utc_ns < self.preflight.observed_utc_ns:
            raise ValueError("terminal lifecycle observation precedes preflight")
        if not isinstance(self.transport_outcome, RadioTransportOutcome):
            raise TypeError("transport_outcome must be a RadioTransportOutcome")
        expected = derive_radio_lifecycle_diagnosis_v0_1(
            self.preflight,
            self.terminal,
            transport_outcome=self.transport_outcome,
        )
        if self.diagnosis != expected:
            raise ValueError("attempt lifecycle diagnosis contradicts its evidence")


@dataclass(frozen=True)
class CaptureBatchLifecycleFactV0_1:
    schema: SchemaRef
    batch_id: CaptureBatchId
    attempts: tuple[CaptureAttemptLifecycleFactV0_1, CaptureAttemptLifecycleFactV0_1]

    SCHEMA_ID = "org.leo-flow.capture-batch-radio-lifecycle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported batch lifecycle fact schema")
        if not isinstance(self.attempts, tuple) or len(self.attempts) != 2:
            raise ValueError("batch lifecycle fact requires exactly two attempts")
        if (
            tuple(sorted(self.attempts, key=lambda item: str(item.attempt_id)))
            != self.attempts
        ):
            raise ValueError("batch lifecycle attempts must use canonical ID order")
        if any(item.batch_id != self.batch_id for item in self.attempts):
            raise ValueError("attempt lifecycle fact belongs to a different batch")
        if len({item.attempt_id for item in self.attempts}) != 2:
            raise ValueError("batch lifecycle attempt IDs must be unique")
        if len({item.radio_id for item in self.attempts}) != 2:
            raise ValueError("batch lifecycle radio IDs must be unique")


@dataclass(frozen=True)
class RadioLifecycleIntervalFactV0_1:
    """Immutable comparison from one slot terminal to the next preflight."""

    schema: SchemaRef
    radio_id: RadioId
    previous_attempt_id: CaptureAttemptId
    current_attempt_id: CaptureAttemptId
    previous_terminal: RadioLifecycleObservationV0_1
    current_preflight: RadioLifecycleObservationV0_1
    diagnosis: RadioLifecycleDiagnosisV0_1

    SCHEMA_ID = "org.leo-flow.radio-lifecycle-interval"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported lifecycle interval fact schema")
        if self.previous_attempt_id == self.current_attempt_id:
            raise ValueError("interval requires two different attempts")
        if (
            self.previous_terminal.radio_id != self.radio_id
            or self.current_preflight.radio_id != self.radio_id
        ):
            raise ValueError("interval observations must belong to one radio")
        if (
            self.current_preflight.observed_utc_ns
            < self.previous_terminal.observed_utc_ns
        ):
            raise ValueError("current preflight precedes previous terminal")
        expected = derive_radio_lifecycle_diagnosis_v0_1(
            self.previous_terminal,
            self.current_preflight,
            transport_outcome=RadioTransportOutcome.COMPLETE,
        )
        if self.diagnosis != expected:
            raise ValueError("interval lifecycle diagnosis contradicts its evidence")


@dataclass(frozen=True)
class CaptureAttemptLifecycleDashboardViewV0_1:
    """Additive capture-detail API view; no raw diagnostic output is exposed."""

    schema: SchemaRef
    attempt_id: CaptureAttemptId
    radio_id: RadioId
    reason: RadioLifecycleReason | None
    confidence: RadioLifecycleConfidence | None
    evidence_codes: tuple[str, ...]
    preflight_boot_id: str | None
    terminal_boot_id: str | None
    preflight_uptime_ns: int | None
    terminal_uptime_ns: int | None
    observer_available_at_terminal: bool

    SCHEMA_ID = "org.leo-flow.dashboard.capture-attempt-radio-lifecycle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported lifecycle dashboard view schema")
        diagnosis = RadioLifecycleDiagnosisV0_1(
            self.reason, self.confidence, self.evidence_codes
        )
        del diagnosis
        if not isinstance(self.observer_available_at_terminal, bool):
            raise TypeError("observer_available_at_terminal must be boolean")
        for value, field in (
            (self.preflight_uptime_ns, "preflight_uptime_ns"),
            (self.terminal_uptime_ns, "terminal_uptime_ns"),
        ):
            if value is not None:
                _nonnegative_int(value, field)


class RadioLifecycleObserverV0_1(Protocol):
    """Narrow bounded port implemented by a diagnostic or metadata adapter."""

    def observe(
        self, radio_id: RadioId, *, deadline_utc_ns: UtcNs
    ) -> RadioLifecycleObservationV0_1: ...


class RadioLifecycleFactRecorderV0_1(Protocol):
    """Append-only port; exact replay is allowed, conflicting replay is not."""

    def record_attempt(
        self, fact: CaptureAttemptLifecycleFactV0_1
    ) -> CaptureAttemptLifecycleFactV0_1: ...

    def record_interval(
        self, fact: RadioLifecycleIntervalFactV0_1
    ) -> RadioLifecycleIntervalFactV0_1: ...


class RadioLifecycleHistoryV0_1(Protocol):
    """Read the last immutable terminal observation for between-slot checks."""

    def latest_terminal(
        self, radio_id: RadioId
    ) -> tuple[CaptureAttemptId, RadioLifecycleObservationV0_1] | None: ...


class CaptureLifecycleDashboardQueryPortV0_1(Protocol):
    def capture_attempt_radio_lifecycle(
        self, attempt_id: CaptureAttemptId
    ) -> CaptureAttemptLifecycleDashboardViewV0_1: ...


def _diagnosis(
    reason: RadioLifecycleReason,
    confidence: RadioLifecycleConfidence,
    *evidence_codes: str,
) -> RadioLifecycleDiagnosisV0_1:
    return RadioLifecycleDiagnosisV0_1(
        reason, confidence, tuple(sorted(evidence_codes))
    )
