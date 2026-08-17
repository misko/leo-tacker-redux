"""Pure lifecycle classification and immutable in-memory fact reduction."""

from __future__ import annotations

from leo_flow.contracts.core import (
    CaptureAttemptId,
    CaptureBatchId,
    RadioId,
    SchemaRef,
    SchemaVersion,
)
from leo_flow.contracts.radio_lifecycle import (
    CaptureAttemptLifecycleDashboardViewV0_1,
    CaptureAttemptLifecycleFactV0_1,
    RadioLifecycleDiagnosisV0_1,
    RadioLifecycleIntervalFactV0_1,
    RadioLifecycleObservationStatus,
    RadioLifecycleObservationV0_1,
    RadioTransportOutcome,
    derive_radio_lifecycle_diagnosis_v0_1,
)


def classify_radio_lifecycle(
    before: RadioLifecycleObservationV0_1,
    after: RadioLifecycleObservationV0_1,
    *,
    transport_outcome: RadioTransportOutcome,
) -> RadioLifecycleDiagnosisV0_1:
    """Classify only identity evidence; time estimates are never identity keys."""

    return derive_radio_lifecycle_diagnosis_v0_1(
        before, after, transport_outcome=transport_outcome
    )


def build_attempt_lifecycle_fact(
    *,
    schema: SchemaRef,
    batch_id: CaptureBatchId,
    attempt_id: CaptureAttemptId,
    radio_id: RadioId,
    preflight: RadioLifecycleObservationV0_1,
    terminal: RadioLifecycleObservationV0_1,
    transport_outcome: RadioTransportOutcome,
) -> CaptureAttemptLifecycleFactV0_1:
    return CaptureAttemptLifecycleFactV0_1(
        schema,
        batch_id,
        attempt_id,
        radio_id,
        preflight,
        terminal,
        transport_outcome,
        classify_radio_lifecycle(
            preflight, terminal, transport_outcome=transport_outcome
        ),
    )


def build_interval_lifecycle_fact(
    *,
    schema: SchemaRef,
    radio_id: RadioId,
    previous_attempt_id: CaptureAttemptId,
    current_attempt_id: CaptureAttemptId,
    previous_terminal: RadioLifecycleObservationV0_1,
    current_preflight: RadioLifecycleObservationV0_1,
) -> RadioLifecycleIntervalFactV0_1:
    return RadioLifecycleIntervalFactV0_1(
        schema,
        radio_id,
        previous_attempt_id,
        current_attempt_id,
        previous_terminal,
        current_preflight,
        classify_radio_lifecycle(
            previous_terminal,
            current_preflight,
            transport_outcome=RadioTransportOutcome.COMPLETE,
        ),
    )


def lifecycle_dashboard_view(
    fact: CaptureAttemptLifecycleFactV0_1, *, schema: SchemaRef
) -> CaptureAttemptLifecycleDashboardViewV0_1:
    return CaptureAttemptLifecycleDashboardViewV0_1(
        schema,
        fact.attempt_id,
        fact.radio_id,
        fact.diagnosis.reason,
        fact.diagnosis.confidence,
        fact.diagnosis.evidence_codes,
        fact.preflight.boot_id,
        fact.terminal.boot_id,
        fact.preflight.uptime_ns,
        fact.terminal.uptime_ns,
        fact.terminal.status is RadioLifecycleObservationStatus.AVAILABLE,
    )


class InMemoryRadioLifecycleFactRecorderV0_1:
    """Component test adapter modeling append-only idempotent persistence."""

    def __init__(self) -> None:
        self._attempts: dict[object, CaptureAttemptLifecycleFactV0_1] = {}
        self._intervals: dict[
            tuple[object, object, object], RadioLifecycleIntervalFactV0_1
        ] = {}

    def record_attempt(
        self, fact: CaptureAttemptLifecycleFactV0_1
    ) -> CaptureAttemptLifecycleFactV0_1:
        existing = self._attempts.get(fact.attempt_id)
        if existing is not None and existing != fact:
            raise ValueError("attempt lifecycle fact already differs")
        self._attempts[fact.attempt_id] = fact
        return fact

    def record_interval(
        self, fact: RadioLifecycleIntervalFactV0_1
    ) -> RadioLifecycleIntervalFactV0_1:
        key = (fact.radio_id, fact.previous_attempt_id, fact.current_attempt_id)
        existing = self._intervals.get(key)
        if existing is not None and existing != fact:
            raise ValueError("lifecycle interval fact already differs")
        self._intervals[key] = fact
        return fact

    def latest_terminal(
        self, radio_id: RadioId
    ) -> tuple[CaptureAttemptId, RadioLifecycleObservationV0_1] | None:
        matches = [
            fact for fact in self._attempts.values() if fact.radio_id == radio_id
        ]
        if not matches:
            return None
        latest = max(
            matches,
            key=lambda fact: (
                int(fact.terminal.observed_utc_ns),
                str(fact.attempt_id),
            ),
        )
        return latest.attempt_id, latest.terminal

    def capture_attempt_radio_lifecycle(
        self, attempt_id: CaptureAttemptId
    ) -> CaptureAttemptLifecycleDashboardViewV0_1:
        try:
            fact = self._attempts[attempt_id]
        except KeyError as error:
            raise LookupError("capture attempt lifecycle fact was not found") from error
        return lifecycle_dashboard_view(
            fact,
            schema=SchemaRef(
                CaptureAttemptLifecycleDashboardViewV0_1.SCHEMA_ID,
                SchemaVersion(0, 1),
            ),
        )
