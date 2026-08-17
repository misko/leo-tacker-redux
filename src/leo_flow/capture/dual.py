"""Capture-owned execution of exactly two independent or coordinated attempts.

Coordinated mode provides a software readiness barrier and a common release
event.  Its scientific timing evidence is still the measured first-sample UTC
reported by each runner.  It does not claim hardware triggering, zero skew, or
clock synchronization.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from leo_flow.contracts.capture_batch import (
    CaptureAttemptOutcome,
    CaptureAttemptState,
    CaptureBatchDefinition,
    CaptureBatchMode,
    CaptureBatchSnapshot,
    ExpectedCaptureAttempt,
)
from leo_flow.contracts.core import (
    V0_1,
    CaptureAttemptId,
    CaptureBatchId,
    PlanId,
    RadioId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.storage import PublishedRecordingRef

_STARTUP_TIMEOUT = "capture_startup_timeout"
_PEER_STARTUP_TIMEOUT = "capture_peer_startup_timeout"
_PEER_STARTUP_FAILED = "capture_peer_startup_failed"
_FINISH_TIMEOUT = "capture_finish_timeout"
_RUNNER_FAILED = "capture_runner_failed"
_NO_RESULT = "capture_runner_no_result"
_START_GATE_MISSING = "capture_start_gate_missing"
_RELEASE_WINDOW_MISSED = "capture_release_window_missed"
_SLOT_DEADLINE = "capture_slot_deadline"
_IDENTITY_MISMATCH = "capture_identity_mismatch"
_RECORDING_IDENTITY_CONFLICT = "capture_recording_identity_conflict"


class DualCaptureError(RuntimeError):
    """Base error for capture-owned dual execution."""


class DualCaptureConfigurationError(DualCaptureError):
    """The executor cannot route exactly one runner to each expected radio."""


class DualCaptureStateError(DualCaptureError):
    """Durable batch state makes a new execution unsafe."""


class DualCaptureCleanupError(DualCaptureError):
    """At least one runner ignored cancellation beyond the cleanup bound."""


class CaptureAttemptFailureReason(str, Enum):
    """Fixed, sanitized failure facts a trusted attempt runner may report."""

    CHILD_BUILD = "capture_child_build_failed"
    CYCLE_PREFLIGHT = "capture_cycle_preflight_failed"
    HOST_SPOOL_PREFLIGHT = "capture_host_spool_preflight_failed"
    CATALOG_PREFLIGHT = "capture_catalog_preflight_failed"
    RADIO_ATTESTATION = "capture_radio_attestation_failed"
    FIRST_SEGMENT_CONFIGURATION = "capture_first_segment_configuration_failed"
    CAPTURE_ENGINE = "capture_engine_failed"
    RECORDING_PUBLICATION = "capture_recording_publication_failed"
    RECORDING_RESOLUTION = "capture_recording_resolution_failed"
    CHILD_CLEANUP = "capture_child_cleanup_failed"


class CaptureAttemptRunnerFailure(DualCaptureError):
    """A runner failed with one validated, non-sensitive reason code."""

    def __init__(self, reason: CaptureAttemptFailureReason) -> None:
        if not isinstance(reason, CaptureAttemptFailureReason):
            raise TypeError("runner failure reason must be a fixed reason code")
        super().__init__("capture attempt runner failed")
        self.reason = reason


class CaptureAttemptControl(Protocol):
    """Cooperative start and cancellation control supplied to one runner."""

    @property
    def cancelled(self) -> bool: ...

    def ready_and_wait_for_release(self) -> bool:
        """Declare readiness and return false if cancelled before release."""


@dataclass(frozen=True)
class CaptureAttemptRunResult:
    """Successful runner result with measured first-sample timing evidence."""

    schema: SchemaRef
    batch_id: CaptureBatchId
    attempt_id: CaptureAttemptId
    radio_id: RadioId
    plan_id: PlanId
    observed_start_utc_ns: UtcNs
    completed_utc_ns: UtcNs
    recording_ref: PublishedRecordingRef

    SCHEMA_ID = "org.leo-flow.capture-attempt-run-result"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported capture attempt run result schema")
        _require_utc_ns(self.observed_start_utc_ns, "observed_start_utc_ns")
        _require_utc_ns(self.completed_utc_ns, "completed_utc_ns")
        if self.completed_utc_ns < self.observed_start_utc_ns:
            raise ValueError("capture completion precedes its observed sample start")


class CaptureAttemptRunner(Protocol):
    """Prepare, cross the supplied start gate, and execute one exact attempt.

    Implementations must observe ``control.cancelled`` during bounded capture
    work and return promptly when it becomes true.
    """

    def run(
        self,
        attempt: ExpectedCaptureAttempt,
        control: CaptureAttemptControl,
    ) -> CaptureAttemptRunResult: ...


class CoordinatedReleaseAdmission(Protocol):
    """Gate a coordinated common release against one exact requested UTC."""

    def admit(
        self, requested_start_utc_ns: UtcNs, *, cancelled: Callable[[], bool]
    ) -> bool: ...


class UtcCoordinatedReleaseGate:
    """Wait until requested UTC and reject a release outside its lateness bound."""

    def __init__(
        self,
        maximum_lateness_ns: int,
        *,
        now_utc_ns: Callable[[], int] = time.time_ns,
        delay: Callable[[float], None] = time.sleep,
        poll_interval_s: float = 0.01,
    ) -> None:
        if (
            isinstance(maximum_lateness_ns, bool)
            or not isinstance(maximum_lateness_ns, int)
            or maximum_lateness_ns < 0
        ):
            raise ValueError("maximum_lateness_ns must be non-negative")
        _require_timeout(poll_interval_s, "poll_interval_s")
        self._maximum_lateness_ns = maximum_lateness_ns
        self._now_utc_ns = now_utc_ns
        self._delay = delay
        self._poll_interval_s = poll_interval_s

    def admit(
        self, requested_start_utc_ns: UtcNs, *, cancelled: Callable[[], bool]
    ) -> bool:
        requested = int(requested_start_utc_ns)
        while True:
            if cancelled():
                return False
            now = self._now_utc_ns()
            if now > requested + self._maximum_lateness_ns:
                return False
            if now >= requested:
                return True
            self._delay(min((requested - now) / 1e9, self._poll_interval_s))


class CaptureBatchRecorder(Protocol):
    """Narrow public-state port; no persistence representation is exposed."""

    def register(self, definition: CaptureBatchDefinition) -> CaptureBatchSnapshot: ...

    def record(self, outcome: CaptureAttemptOutcome) -> CaptureBatchSnapshot: ...


@dataclass
class _Worker:
    attempt: ExpectedCaptureAttempt
    runner: CaptureAttemptRunner
    control: _AttemptControl
    thread: threading.Thread | None = None
    result: CaptureAttemptRunResult | None = None
    failure_reason: str | None = None


class _AttemptControl:
    def __init__(self, release: threading.Event) -> None:
        self._release = release
        self._ready = threading.Event()
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def ready_and_wait_for_release(self) -> bool:
        self._ready.set()
        while not self._release.wait(0.01):
            if self.cancelled:
                return False
        return not self.cancelled

    def wait_ready(self, timeout_s: float) -> bool:
        return self._ready.wait(timeout_s)

    def cancel(self) -> None:
        self._cancelled.set()


class DualCaptureExecutor:
    """Run exactly two attempts and reduce both to immutable terminal outcomes."""

    def __init__(
        self,
        recorder: CaptureBatchRecorder,
        *,
        startup_timeout_s: float,
        finish_timeout_s: float,
        cleanup_timeout_s: float,
        now_utc_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
        coordinated_release_admission: CoordinatedReleaseAdmission | None = None,
    ) -> None:
        _require_timeout(startup_timeout_s, "startup_timeout_s")
        _require_timeout(finish_timeout_s, "finish_timeout_s")
        _require_timeout(cleanup_timeout_s, "cleanup_timeout_s")
        self._recorder = recorder
        self._startup_timeout_s = startup_timeout_s
        self._finish_timeout_s = finish_timeout_s
        self._cleanup_timeout_s = cleanup_timeout_s
        self._now_utc_ns = now_utc_ns
        self._monotonic = monotonic
        self._coordinated_release_admission = coordinated_release_admission

    def execute(
        self,
        definition: CaptureBatchDefinition,
        runners: Mapping[RadioId, CaptureAttemptRunner],
        *,
        deadline_utc_ns: UtcNs | None = None,
    ) -> CaptureBatchSnapshot:
        expected_radios = {item.radio_id for item in definition.expected_attempts}
        if set(runners) != expected_radios:
            raise DualCaptureConfigurationError(
                "dual capture requires exactly one runner per expected radio"
            )
        initial = self._recorder.register(definition)
        if initial.definition != definition:
            raise DualCaptureStateError("batch recorder returned another definition")
        if initial.outcomes:
            if initial.terminal:
                return initial
            raise DualCaptureStateError(
                "partially terminal batch cannot be recaptured by this executor"
            )
        execution_deadline: float | None = None
        if deadline_utc_ns is not None:
            _require_utc_ns(deadline_utc_ns, "deadline_utc_ns")
            remaining_s = (int(deadline_utc_ns) - self._now_utc_ns()) / 1e9
            if remaining_s <= 0:
                state = initial
                for attempt in definition.expected_attempts:
                    state = self._recorder.record(
                        self._outcome(
                            definition.batch_id, attempt, None, _SLOT_DEADLINE
                        )
                    )
                return state
            execution_deadline = self._monotonic() + remaining_s

        coordinated = definition.mode is CaptureBatchMode.COORDINATED
        common_release = threading.Event()
        first_attempt, second_attempt = definition.expected_attempts
        workers = (
            _Worker(
                first_attempt,
                runners[first_attempt.radio_id],
                _AttemptControl(common_release if coordinated else _released_event()),
            ),
            _Worker(
                second_attempt,
                runners[second_attempt.radio_id],
                _AttemptControl(common_release if coordinated else _released_event()),
            ),
        )
        for worker in workers:
            worker.thread = threading.Thread(
                target=self._run,
                args=(worker,),
                name=f"dual-capture-{worker.attempt.attempt_id}",
                daemon=True,
            )
            worker.thread.start()

        all_ready, startup_expired, execution_expired = self._wait_for_start(
            workers, execution_deadline
        )
        startup_failures: dict[CaptureAttemptId, str] = {}
        if all_ready:
            if coordinated:
                release_admitted = True
                if self._coordinated_release_admission is not None:
                    try:
                        release_admitted = self._coordinated_release_admission.admit(
                            first_attempt.requested_start_utc_ns,
                            cancelled=lambda: any(
                                worker.control.cancelled for worker in workers
                            ),
                        )
                    except Exception:  # noqa: BLE001 - fail closed at release
                        release_admitted = False
                if (
                    execution_deadline is not None
                    and self._monotonic() >= execution_deadline
                ):
                    release_admitted = False
                if release_admitted:
                    common_release.set()
                else:
                    for worker in workers:
                        worker.control.cancel()
                        startup_failures[worker.attempt.attempt_id] = (
                            _SLOT_DEADLINE
                            if execution_deadline is not None
                            and self._monotonic() >= execution_deadline
                            else _RELEASE_WINDOW_MISSED
                        )
                    common_release.set()
        elif coordinated:
            unready_live = any(
                not worker.control.ready and _is_alive(worker) for worker in workers
            )
            for worker in workers:
                worker.control.cancel()
                if execution_expired:
                    startup_failures[worker.attempt.attempt_id] = _SLOT_DEADLINE
                elif worker.control.ready:
                    startup_failures[worker.attempt.attempt_id] = (
                        _PEER_STARTUP_TIMEOUT
                        if unready_live and startup_expired
                        else _PEER_STARTUP_FAILED
                    )
                elif _is_alive(worker) and startup_expired:
                    startup_failures[worker.attempt.attempt_id] = (
                        _SLOT_DEADLINE if execution_expired else _STARTUP_TIMEOUT
                    )
            common_release.set()
        else:
            for worker in workers:
                if not worker.control.ready and _is_alive(worker) and startup_expired:
                    worker.control.cancel()
                    startup_failures[worker.attempt.attempt_id] = (
                        _SLOT_DEADLINE if execution_expired else _STARTUP_TIMEOUT
                    )

        finish_deadline = self._monotonic() + self._finish_timeout_s
        if execution_deadline is not None:
            finish_deadline = min(finish_deadline, execution_deadline)
        for worker in workers:
            thread = _thread(worker)
            thread.join(max(0.0, finish_deadline - self._monotonic()))
        finish_timeouts = {
            worker.attempt.attempt_id for worker in workers if _is_alive(worker)
        }
        slot_timeouts = (
            finish_timeouts
            if execution_deadline is not None
            and self._monotonic() >= execution_deadline
            else set()
        )
        for worker in workers:
            if worker.attempt.attempt_id in finish_timeouts:
                worker.control.cancel()

        cleanup_deadline = self._monotonic() + self._cleanup_timeout_s
        for worker in workers:
            if _is_alive(worker):
                _thread(worker).join(max(0.0, cleanup_deadline - self._monotonic()))
        cleanup_failures = tuple(worker for worker in workers if _is_alive(worker))

        outcomes = self._outcomes(
            definition,
            workers,
            startup_failures,
            finish_timeouts,
            slot_timeouts,
        )
        state = initial
        for outcome in outcomes:
            state = self._recorder.record(outcome)
            if state.definition != definition or outcome not in state.outcomes:
                raise DualCaptureStateError(
                    "batch recorder changed or omitted a terminal outcome"
                )
        if not state.terminal:
            raise DualCaptureStateError(
                "dual capture did not reach terminal batch state"
            )
        if cleanup_failures:
            raise DualCaptureCleanupError(
                "capture runner ignored cancellation beyond cleanup timeout"
            )
        return state

    def _wait_for_start(
        self,
        workers: tuple[_Worker, _Worker],
        execution_deadline: float | None,
    ) -> tuple[bool, bool, bool]:
        deadline = self._monotonic() + self._startup_timeout_s
        if execution_deadline is not None:
            deadline = min(deadline, execution_deadline)
        while True:
            if all(worker.control.ready for worker in workers):
                return True, False, False
            if any(
                not worker.control.ready and not _is_alive(worker) for worker in workers
            ):
                return False, False, False
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return (
                    False,
                    True,
                    execution_deadline is not None
                    and self._monotonic() >= execution_deadline,
                )
            for worker in workers:
                if not worker.control.ready:
                    worker.control.wait_ready(min(remaining, 0.01))
                    break

    @staticmethod
    def _run(worker: _Worker) -> None:
        try:
            worker.result = worker.runner.run(worker.attempt, worker.control)
        except CaptureAttemptRunnerFailure as error:
            worker.failure_reason = error.reason.value
        except Exception:  # noqa: BLE001 - runner exceptions become bounded facts
            worker.failure_reason = _RUNNER_FAILED

    def _outcomes(
        self,
        definition: CaptureBatchDefinition,
        workers: tuple[_Worker, _Worker],
        startup_failures: Mapping[CaptureAttemptId, str],
        finish_timeouts: set[CaptureAttemptId],
        slot_timeouts: set[CaptureAttemptId],
    ) -> tuple[CaptureAttemptOutcome, CaptureAttemptOutcome]:
        failures = dict(startup_failures)
        valid_results: dict[CaptureAttemptId, CaptureAttemptRunResult] = {}
        for worker in workers:
            attempt_id = worker.attempt.attempt_id
            if attempt_id in failures:
                continue
            if attempt_id in finish_timeouts:
                failures[attempt_id] = (
                    _SLOT_DEADLINE if attempt_id in slot_timeouts else _FINISH_TIMEOUT
                )
                continue
            if worker.failure_reason is not None:
                failures[attempt_id] = worker.failure_reason
                continue
            result = worker.result
            if result is None:
                failures[attempt_id] = _NO_RESULT
                continue
            if not worker.control.ready:
                failures[attempt_id] = _START_GATE_MISSING
                continue
            if not _matches(definition.batch_id, worker.attempt, result):
                failures[attempt_id] = _IDENTITY_MISMATCH
                continue
            valid_results[attempt_id] = result

        by_recording: dict[object, list[CaptureAttemptId]] = {}
        for attempt_id, result in valid_results.items():
            by_recording.setdefault(result.recording_ref.recording_id, []).append(
                attempt_id
            )
        for attempt_ids in by_recording.values():
            if len(attempt_ids) > 1:
                for attempt_id in attempt_ids:
                    failures[attempt_id] = _RECORDING_IDENTITY_CONFLICT
                    valid_results.pop(attempt_id)

        return (
            self._outcome(
                definition.batch_id,
                workers[0].attempt,
                valid_results.get(workers[0].attempt.attempt_id),
                failures.get(workers[0].attempt.attempt_id),
            ),
            self._outcome(
                definition.batch_id,
                workers[1].attempt,
                valid_results.get(workers[1].attempt.attempt_id),
                failures.get(workers[1].attempt.attempt_id),
            ),
        )

    def _outcome(
        self,
        batch_id: CaptureBatchId,
        attempt: ExpectedCaptureAttempt,
        result: CaptureAttemptRunResult | None,
        failure: str | None,
    ) -> CaptureAttemptOutcome:
        if result is not None and failure is None:
            return CaptureAttemptOutcome(
                SchemaRef(CaptureAttemptOutcome.SCHEMA_ID),
                batch_id,
                attempt.attempt_id,
                attempt.radio_id,
                attempt.plan_id,
                CaptureAttemptState.SUCCEEDED,
                result.completed_utc_ns,
                result.observed_start_utc_ns,
                result.recording_ref,
            )
        return CaptureAttemptOutcome(
            SchemaRef(CaptureAttemptOutcome.SCHEMA_ID),
            batch_id,
            attempt.attempt_id,
            attempt.radio_id,
            attempt.plan_id,
            CaptureAttemptState.FAILED,
            UtcNs(self._now_utc_ns()),
            failure_reason=failure or _NO_RESULT,
        )


def _matches(
    batch_id: CaptureBatchId,
    attempt: ExpectedCaptureAttempt,
    result: CaptureAttemptRunResult,
) -> bool:
    return (
        result.batch_id == batch_id
        and result.attempt_id == attempt.attempt_id
        and result.radio_id == attempt.radio_id
        and result.plan_id == attempt.plan_id
    )


def _released_event() -> threading.Event:
    event = threading.Event()
    event.set()
    return event


def _thread(worker: _Worker) -> threading.Thread:
    assert worker.thread is not None
    return worker.thread


def _is_alive(worker: _Worker) -> bool:
    return _thread(worker).is_alive()


def _require_timeout(value: float, field: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field} must be a finite positive number")


def _require_utc_ns(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative UTC nanosecond integer")
