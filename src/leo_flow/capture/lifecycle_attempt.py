"""Lifecycle observation decorator for one isolated capture attempt."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from leo_flow.contracts.capture_batch import ExpectedCaptureAttempt
from leo_flow.contracts.core import V0_1, CaptureBatchId, RadioId, SchemaRef, UtcNs
from leo_flow.contracts.radio_lifecycle import (
    CaptureAttemptLifecycleFactV0_1,
    RadioLifecycleFactRecorderV0_1,
    RadioLifecycleHistoryV0_1,
    RadioLifecycleIntervalFactV0_1,
    RadioLifecycleObservationV0_1,
    RadioLifecycleObserverV0_1,
    RadioTransportOutcome,
)

from .dual import CaptureAttemptRunResult
from .radio_lifecycle import build_attempt_lifecycle_fact, build_interval_lifecycle_fact


class AttemptWorkV0_1(Protocol):
    def preflight(self) -> None: ...
    def capture(self, attempt: ExpectedCaptureAttempt) -> CaptureAttemptRunResult: ...
    def close(self, timeout_s: float) -> None: ...


class LifecycleObservedAttemptWorkV0_1:
    """Observe before readiness and after every terminal/failure boundary.

    The observer deadline is fixed and bounded.  Observation/persistence errors
    never replace the capture error, but they do fail an otherwise successful
    attempt because claiming lifecycle coverage without a durable fact is unsafe.
    """

    def __init__(
        self,
        delegate: AttemptWorkV0_1,
        *,
        batch_id: CaptureBatchId,
        radio_id: RadioId,
        observer: RadioLifecycleObserverV0_1,
        recorder: RadioLifecycleFactRecorderV0_1,
        history: RadioLifecycleHistoryV0_1,
        utc_now_ns: Callable[[], int],
        observation_timeout_ns: int = 2_000_000_000,
    ) -> None:
        if observation_timeout_ns <= 0:
            raise ValueError("lifecycle observation timeout must be positive")
        self._delegate = delegate
        self._batch_id = batch_id
        self._radio_id = radio_id
        self._observer = observer
        self._recorder = recorder
        self._history = history
        self._utc_now_ns = utc_now_ns
        self._timeout_ns = observation_timeout_ns
        self._preflight: RadioLifecycleObservationV0_1 | None = None
        self._attempt: ExpectedCaptureAttempt | None = None
        self._outcome = RadioTransportOutcome.OTHER_FAILURE
        self._sealed = False

    def preflight(self) -> None:
        now = max(0, int(self._utc_now_ns()))
        self._preflight = self._observer.observe(
            self._radio_id, deadline_utc_ns=UtcNs(now + self._timeout_ns)
        )
        # Re-attest the complete radio (including passive TX) after observing
        # any between-slot lifecycle change and before declaring readiness.
        self._delegate.preflight()

    def capture(self, attempt: ExpectedCaptureAttempt) -> CaptureAttemptRunResult:
        self._attempt = attempt
        if attempt.radio_id != self._radio_id or self._preflight is None:
            raise RuntimeError("lifecycle preflight does not match capture attempt")
        previous = self._history.latest_terminal(attempt.radio_id)
        if previous is not None:
            previous_attempt_id, terminal = previous
            interval = build_interval_lifecycle_fact(
                schema=SchemaRef(RadioLifecycleIntervalFactV0_1.SCHEMA_ID, V0_1),
                radio_id=attempt.radio_id,
                previous_attempt_id=previous_attempt_id,
                current_attempt_id=attempt.attempt_id,
                previous_terminal=terminal,
                current_preflight=self._preflight,
            )
            self._recorder.record_interval(interval)
        try:
            result = self._delegate.capture(attempt)
        except TimeoutError:
            self._outcome = RadioTransportOutcome.TIMEOUT
            raise
        except (BrokenPipeError, ConnectionError, EOFError):
            self._outcome = RadioTransportOutcome.DISCONNECTED
            raise
        except BaseException:
            self._outcome = RadioTransportOutcome.OTHER_FAILURE
            raise
        self._outcome = RadioTransportOutcome.COMPLETE
        return result

    def close(self, timeout_s: float) -> None:
        failure: BaseException | None = None
        try:
            self._delegate.close(timeout_s)
        except Exception as error:  # noqa: BLE001 - preserve cleanup failure
            failure = error
            if self._outcome is RadioTransportOutcome.COMPLETE:
                self._outcome = RadioTransportOutcome.OTHER_FAILURE
        try:
            self._seal()
        except Exception:
            if failure is None:
                raise
        if failure is not None:
            raise failure

    def _observe(
        self, attempt: ExpectedCaptureAttempt
    ) -> RadioLifecycleObservationV0_1:
        now = max(0, int(self._utc_now_ns()))
        return self._observer.observe(
            attempt.radio_id, deadline_utc_ns=UtcNs(now + self._timeout_ns)
        )

    def _seal(self) -> CaptureAttemptLifecycleFactV0_1 | None:
        if self._sealed or self._attempt is None or self._preflight is None:
            return None
        terminal = self._observe(self._attempt)
        fact = build_attempt_lifecycle_fact(
            schema=SchemaRef(CaptureAttemptLifecycleFactV0_1.SCHEMA_ID, V0_1),
            batch_id=self._batch_id,
            attempt_id=self._attempt.attempt_id,
            radio_id=self._attempt.radio_id,
            preflight=self._preflight,
            terminal=terminal,
            transport_outcome=self._outcome,
        )
        recorded = self._recorder.record_attempt(fact)
        self._sealed = True
        return recorded
