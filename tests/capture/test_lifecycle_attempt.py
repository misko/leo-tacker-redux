from __future__ import annotations

from dataclasses import dataclass

import pytest

from leo_flow.capture.lifecycle_attempt import LifecycleObservedAttemptWorkV0_1
from leo_flow.capture.radio_lifecycle import InMemoryRadioLifecycleFactRecorderV0_1
from leo_flow.contracts.capture_batch import ExpectedCaptureAttempt
from leo_flow.contracts.core import (
    V0_1,
    CaptureAttemptId,
    CaptureBatchId,
    PlanId,
    RadioId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.radio_lifecycle import (
    IiodProcessIdentityV0_1,
    RadioLifecycleObservationSource,
    RadioLifecycleObservationStatus,
    RadioLifecycleObservationV0_1,
    RadioLifecycleTrust,
    RadioTransportOutcome,
)

RADIO = RadioId("radio_lifecycle_work")
ATTEMPT = ExpectedCaptureAttempt(
    CaptureAttemptId("cattempt_lifecycle_work"),
    RADIO,
    PlanId("plan_lifecycle_work"),
    UtcNs(100),
)


def _observation(observed: int, boot: str) -> RadioLifecycleObservationV0_1:
    return RadioLifecycleObservationV0_1(
        SchemaRef(RadioLifecycleObservationV0_1.SCHEMA_ID, V0_1),
        RADIO,
        UtcNs(observed),
        RadioLifecycleObservationStatus.AVAILABLE,
        RadioLifecycleObservationSource.AUTHENTICATED_DIAGNOSTIC_V1,
        RadioLifecycleTrust.RADIO_AUTHENTICATED,
        boot,
        observed - 1,
        UtcNs(1),
        1,
        IiodProcessIdentityV0_1(3, 4, 100),
    )


class _Observer:
    def __init__(self, values, events) -> None:
        self.values = iter(values)
        self.events = events

    def observe(self, radio_id: RadioId, *, deadline_utc_ns: UtcNs):
        assert radio_id == RADIO and int(deadline_utc_ns) > 0
        self.events.append("observe")
        return next(self.values)


class _History:
    def latest_terminal(self, radio_id: RadioId):
        assert radio_id == RADIO


@dataclass
class _Work:
    events: list[str]
    failure: BaseException | None = None

    def preflight(self) -> None:
        self.events.append("attest")

    def capture(self, attempt):
        self.events.append("capture")
        if self.failure is not None:
            raise self.failure
        return object()

    def close(self, timeout_s: float) -> None:
        self.events.append("close")


def _decorated(work, recorder, observer):
    return LifecycleObservedAttemptWorkV0_1(
        work,
        batch_id=CaptureBatchId("cbatch_lifecycle_work"),
        radio_id=RADIO,
        observer=observer,
        recorder=recorder,
        history=_History(),
        utc_now_ns=lambda: 10,
    )


def test_observation_precedes_radio_attestation_and_terminal_fact_is_durable() -> None:
    events: list[str] = []
    recorder = InMemoryRadioLifecycleFactRecorderV0_1()
    work = _decorated(
        _Work(events),
        recorder,
        _Observer(
            [
                _observation(10, "41974bfd-7aa8-4d28-b1c8-57d21c3e05bb"),
                _observation(20, "41974bfd-7aa8-4d28-b1c8-57d21c3e05bb"),
            ],
            events,
        ),
    )
    work.preflight()
    work.capture(ATTEMPT)
    work.close(10)
    assert events == ["observe", "attest", "capture", "close", "observe"]
    fact = recorder._attempts[ATTEMPT.attempt_id]
    assert fact.transport_outcome is RadioTransportOutcome.COMPLETE


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [
        (TimeoutError(), RadioTransportOutcome.TIMEOUT),
        (BrokenPipeError(), RadioTransportOutcome.DISCONNECTED),
        (RuntimeError(), RadioTransportOutcome.OTHER_FAILURE),
    ],
)
def test_failure_boundary_is_observed_and_original_failure_survives(
    failure, outcome
) -> None:
    events: list[str] = []
    recorder = InMemoryRadioLifecycleFactRecorderV0_1()
    work = _decorated(
        _Work(events, failure),
        recorder,
        _Observer(
            [
                _observation(10, "41974bfd-7aa8-4d28-b1c8-57d21c3e05bb"),
                _observation(20, "d6f89d3a-6856-441f-83db-96c71728e15b"),
            ],
            events,
        ),
    )
    work.preflight()
    with pytest.raises(type(failure)):
        work.capture(ATTEMPT)
    work.close(10)
    fact = recorder._attempts[ATTEMPT.attempt_id]
    assert fact.transport_outcome is outcome
    assert fact.diagnosis.reason.value == "radio_rebooted"
