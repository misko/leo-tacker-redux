"""Idempotent two-radio batch reduction and paired-analysis admission."""

from __future__ import annotations

import threading
from typing import Protocol

from leo_flow.contracts.capture_batch import (
    AdmittedCapture,
    CaptureAttemptOutcome,
    CaptureBatchDefinition,
    CaptureBatchSnapshot,
    PairedAnalysisEligibility,
    PairedCaptureAdmission,
)
from leo_flow.contracts.core import CaptureBatchId, SchemaRef


class CaptureBatchError(RuntimeError):
    """Base error for capture batch coordination."""


class CaptureBatchNotFound(CaptureBatchError):
    """No batch exists for the exact requested identity."""


class CaptureBatchIdentityConflict(CaptureBatchError):
    """An immutable batch or attempt identity was reused differently."""


class CaptureBatchRevisionConflict(CaptureBatchError):
    """The stored batch changed since it was read."""


class CaptureBatchNotEligible(CaptureBatchError):
    """The batch cannot be admitted to paired analysis."""


class CaptureBatchStateStore(Protocol):
    """Narrow compare-and-swap port; implementations own their persistence."""

    def create(self, initial: CaptureBatchSnapshot) -> CaptureBatchSnapshot: ...

    def get(self, batch_id: CaptureBatchId) -> CaptureBatchSnapshot | None: ...

    def compare_and_swap(
        self,
        batch_id: CaptureBatchId,
        expected_revision: int,
        replacement: CaptureBatchSnapshot,
    ) -> CaptureBatchSnapshot: ...


class InMemoryCaptureBatchStateStore:
    """Thread-safe semantic store for local composition and component tests."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[CaptureBatchId, CaptureBatchSnapshot] = {}

    def create(self, initial: CaptureBatchSnapshot) -> CaptureBatchSnapshot:
        with self._lock:
            current = self._states.get(initial.batch_id)
            if current is None:
                self._states[initial.batch_id] = initial
                return initial
            if current.definition != initial.definition:
                raise CaptureBatchIdentityConflict(
                    "batch ID already identifies another definition"
                )
            return current

    def get(self, batch_id: CaptureBatchId) -> CaptureBatchSnapshot | None:
        with self._lock:
            return self._states.get(batch_id)

    def compare_and_swap(
        self,
        batch_id: CaptureBatchId,
        expected_revision: int,
        replacement: CaptureBatchSnapshot,
    ) -> CaptureBatchSnapshot:
        with self._lock:
            current = self._states.get(batch_id)
            if current is None:
                raise CaptureBatchNotFound(f"capture batch {batch_id} was not found")
            if current.revision != expected_revision:
                raise CaptureBatchRevisionConflict("capture batch revision changed")
            if replacement.batch_id != batch_id or (
                replacement.revision != expected_revision + 1
            ):
                raise CaptureBatchIdentityConflict(
                    "replacement has an invalid batch identity or revision"
                )
            if replacement.definition != current.definition:
                raise CaptureBatchIdentityConflict(
                    "replacement changed the immutable batch definition"
                )
            self._states[batch_id] = replacement
            return replacement


class CaptureBatchCoordinator:
    """Reduce terminal attempt facts and admit only an exact complete pair."""

    def __init__(
        self, store: CaptureBatchStateStore, *, maximum_revision_retries: int = 8
    ) -> None:
        if (
            isinstance(maximum_revision_retries, bool)
            or not isinstance(maximum_revision_retries, int)
            or maximum_revision_retries <= 0
        ):
            raise ValueError("maximum_revision_retries must be positive")
        self._store = store
        self._maximum_revision_retries = maximum_revision_retries

    def register(self, definition: CaptureBatchDefinition) -> CaptureBatchSnapshot:
        initial = CaptureBatchSnapshot(
            SchemaRef(CaptureBatchSnapshot.SCHEMA_ID), definition
        )
        state = self._store.create(initial)
        if state.batch_id != definition.batch_id or state.definition != definition:
            raise CaptureBatchIdentityConflict(
                "batch store returned another immutable definition"
            )
        return state

    def inspect(self, batch_id: CaptureBatchId) -> CaptureBatchSnapshot:
        state = self._store.get(batch_id)
        if state is None:
            raise CaptureBatchNotFound(f"capture batch {batch_id} was not found")
        return state

    def record(self, outcome: CaptureAttemptOutcome) -> CaptureBatchSnapshot:
        for _ in range(self._maximum_revision_retries):
            state = self._store.get(outcome.batch_id)
            if state is None:
                raise CaptureBatchNotFound(
                    f"no capture batch expects attempt {outcome.attempt_id}"
                )
            try:
                replacement = state.record(outcome)
            except ValueError as error:
                raise CaptureBatchIdentityConflict(str(error)) from error
            if replacement is state:
                return state
            try:
                stored = self._store.compare_and_swap(
                    state.batch_id, state.revision, replacement
                )
            except CaptureBatchRevisionConflict:
                continue
            if stored != replacement:
                raise CaptureBatchIdentityConflict(
                    "batch store changed the proposed terminal outcome"
                )
            return stored
        raise CaptureBatchRevisionConflict("capture batch retry limit was exhausted")

    def admit_paired_analysis(self, batch_id: CaptureBatchId) -> PairedCaptureAdmission:
        """Authorize exact pair comparison with mode-specific timing semantics."""

        state = self.inspect(batch_id)
        if state.paired_analysis_eligibility is not PairedAnalysisEligibility.ELIGIBLE:
            raise CaptureBatchNotEligible(
                f"capture batch {batch_id} is {state.paired_analysis_eligibility.value}"
            )
        observed = state.observed_start_skew_ns
        if observed is None:
            raise CaptureBatchNotEligible("capture batch has no observed start skew")
        by_attempt = {item.attempt_id: item for item in state.outcomes}
        first_expected, second_expected = state.definition.expected_attempts
        first_outcome = by_attempt[first_expected.attempt_id]
        second_outcome = by_attempt[second_expected.attempt_id]
        first_recording = first_outcome.recording_ref
        second_recording = second_outcome.recording_ref
        if first_recording is None or second_recording is None:
            raise CaptureBatchNotEligible(
                "capture batch has no complete recording pair"
            )
        captures = (
            AdmittedCapture(
                first_expected.attempt_id,
                first_expected.radio_id,
                first_expected.plan_id,
                first_recording,
            ),
            AdmittedCapture(
                second_expected.attempt_id,
                second_expected.radio_id,
                second_expected.plan_id,
                second_recording,
            ),
        )
        return PairedCaptureAdmission(
            SchemaRef(PairedCaptureAdmission.SCHEMA_ID),
            batch_id,
            state.definition.mode,
            captures,
            state.requested_start_skew_ns,
            observed,
            state.definition.maximum_observed_start_skew_ns,
        )
