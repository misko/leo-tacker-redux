"""Public immutable contracts for two-radio capture batches."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum

from ._validation import require_utc_ns
from .core import (
    V0_1,
    CaptureAttemptId,
    CaptureBatchId,
    PlanId,
    RadioId,
    SchemaRef,
    UtcNs,
)
from .storage import PublishedRecordingRef

_FAILURE_REASON = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")


def _require_nonnegative_integer(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")


class CaptureBatchMode(str, Enum):
    INDEPENDENT = "independent"
    COORDINATED = "coordinated"


class CaptureAttemptState(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PairedAnalysisEligibility(str, Enum):
    PENDING = "pending"
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"


@dataclass(frozen=True)
class ExpectedCaptureAttempt:
    attempt_id: CaptureAttemptId
    radio_id: RadioId
    plan_id: PlanId
    requested_start_utc_ns: UtcNs

    def __post_init__(self) -> None:
        require_utc_ns(self.requested_start_utc_ns, "requested_start_utc_ns")


@dataclass(frozen=True)
class CaptureBatchDefinition:
    """The complete, immutable intent for exactly two capture attempts.

    The coordinated-mode skew limit applies to measured first-sample times,
    never to command-dispatch or function-entry timestamps.
    """

    schema: SchemaRef
    batch_id: CaptureBatchId
    mode: CaptureBatchMode
    expected_attempts: tuple[ExpectedCaptureAttempt, ExpectedCaptureAttempt]
    maximum_observed_start_skew_ns: int | None = None

    SCHEMA_ID = "org.leo-flow.capture-batch"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported capture batch schema")
        if not isinstance(self.mode, CaptureBatchMode):
            raise TypeError("capture batch mode must be a CaptureBatchMode")
        if (
            not isinstance(self.expected_attempts, tuple)
            or len(self.expected_attempts) != 2
        ):
            raise ValueError("capture batch requires exactly two expected attempts")
        if tuple(
            sorted(self.expected_attempts, key=lambda item: str(item.attempt_id))
        ) != (self.expected_attempts):
            raise ValueError("expected capture attempts must use canonical ID order")
        for field, values in (
            ("attempt IDs", tuple(item.attempt_id for item in self.expected_attempts)),
            ("radio IDs", tuple(item.radio_id for item in self.expected_attempts)),
            ("plan IDs", tuple(item.plan_id for item in self.expected_attempts)),
        ):
            if len(set(values)) != 2:
                raise ValueError(f"capture batch {field} must be unique")
        if self.mode is CaptureBatchMode.INDEPENDENT:
            if self.maximum_observed_start_skew_ns is not None:
                raise ValueError("independent capture cannot declare a skew limit")
        else:
            if self.maximum_observed_start_skew_ns is None:
                raise ValueError("coordinated capture requires an observed skew limit")
            _require_nonnegative_integer(
                self.maximum_observed_start_skew_ns,
                "maximum_observed_start_skew_ns",
            )
            if self.requested_start_skew_ns != 0:
                raise ValueError("coordinated capture requires one requested start")

    @property
    def requested_start_skew_ns(self) -> int:
        first, second = self.expected_attempts
        return abs(
            int(first.requested_start_utc_ns) - int(second.requested_start_utc_ns)
        )


@dataclass(frozen=True)
class CaptureAttemptOutcome:
    """One immutable terminal fact; retries must repeat this exact value.

    ``observed_start_utc_ns`` is the measured time of the first accepted sample.
    It is not the time at which software requested or dispatched acquisition.
    """

    schema: SchemaRef
    batch_id: CaptureBatchId
    attempt_id: CaptureAttemptId
    radio_id: RadioId
    plan_id: PlanId
    state: CaptureAttemptState
    terminal_utc_ns: UtcNs
    observed_start_utc_ns: UtcNs | None = None
    recording_ref: PublishedRecordingRef | None = None
    failure_reason: str | None = None

    SCHEMA_ID = "org.leo-flow.capture-attempt-outcome"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported capture attempt outcome schema")
        if not isinstance(self.state, CaptureAttemptState):
            raise TypeError("capture attempt state must be a CaptureAttemptState")
        require_utc_ns(self.terminal_utc_ns, "terminal_utc_ns")
        if self.observed_start_utc_ns is not None:
            require_utc_ns(self.observed_start_utc_ns, "observed_start_utc_ns")
            if self.terminal_utc_ns < self.observed_start_utc_ns:
                raise ValueError("capture attempt terminates before its observed start")
        if self.state is CaptureAttemptState.SUCCEEDED:
            if self.observed_start_utc_ns is None or self.recording_ref is None:
                raise ValueError(
                    "successful capture requires an observed start and recording"
                )
            if self.failure_reason is not None:
                raise ValueError("successful capture cannot declare a failure reason")
        else:
            if self.recording_ref is not None:
                raise ValueError("failed capture cannot publish a recording")
            if (
                self.failure_reason is None
                or _FAILURE_REASON.fullmatch(self.failure_reason) is None
            ):
                raise ValueError("failed capture requires a bounded reason code")


@dataclass(frozen=True)
class CaptureBatchSnapshot:
    """Revisioned public state reduced from immutable terminal outcomes."""

    schema: SchemaRef
    definition: CaptureBatchDefinition
    outcomes: tuple[CaptureAttemptOutcome, ...] = ()
    revision: int = 0

    SCHEMA_ID = "org.leo-flow.capture-batch-state"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported capture batch state schema")
        if not isinstance(self.outcomes, tuple):
            raise TypeError("capture batch outcomes must be an immutable tuple")
        _require_nonnegative_integer(self.revision, "revision")
        if self.revision != len(self.outcomes):
            raise ValueError("batch revision must equal its terminal outcome count")
        expected = {
            item.attempt_id: (index, item)
            for index, item in enumerate(self.definition.expected_attempts)
        }
        seen: set[CaptureAttemptId] = set()
        prior_index = -1
        recording_ids = []
        for outcome in self.outcomes:
            if outcome.attempt_id in seen or outcome.attempt_id not in expected:
                raise ValueError(
                    "batch outcomes must identify unique expected attempts"
                )
            if outcome.batch_id != self.batch_id:
                raise ValueError("capture outcome belongs to another batch")
            index, attempt = expected[outcome.attempt_id]
            if index <= prior_index:
                raise ValueError("batch outcomes must use expected-attempt order")
            if (
                outcome.radio_id != attempt.radio_id
                or outcome.plan_id != attempt.plan_id
            ):
                raise ValueError("capture outcome is routed to another radio or plan")
            if outcome.recording_ref is not None:
                recording_ids.append(outcome.recording_ref.recording_id)
            seen.add(outcome.attempt_id)
            prior_index = index
        if len(recording_ids) != len(set(recording_ids)):
            raise ValueError("successful attempts must publish different recordings")

    @property
    def batch_id(self) -> CaptureBatchId:
        return self.definition.batch_id

    @property
    def terminal(self) -> bool:
        return len(self.outcomes) == len(self.definition.expected_attempts)

    @property
    def requested_start_skew_ns(self) -> int:
        return self.definition.requested_start_skew_ns

    @property
    def observed_start_skew_ns(self) -> int | None:
        if len(self.outcomes) != 2 or any(
            item.observed_start_utc_ns is None for item in self.outcomes
        ):
            return None
        first, second = self.outcomes
        assert first.observed_start_utc_ns is not None
        assert second.observed_start_utc_ns is not None
        return abs(int(first.observed_start_utc_ns) - int(second.observed_start_utc_ns))

    @property
    def successful_recordings(self) -> tuple[PublishedRecordingRef, ...]:
        return tuple(
            outcome.recording_ref
            for outcome in self.outcomes
            if outcome.recording_ref is not None
        )

    @property
    def paired_analysis_eligibility(self) -> PairedAnalysisEligibility:
        if not self.terminal:
            return PairedAnalysisEligibility.PENDING
        if any(
            item.state is not CaptureAttemptState.SUCCEEDED for item in self.outcomes
        ):
            return PairedAnalysisEligibility.INELIGIBLE
        if self.definition.mode is CaptureBatchMode.COORDINATED:
            observed = self.observed_start_skew_ns
            maximum = self.definition.maximum_observed_start_skew_ns
            if observed is None or maximum is None or observed > maximum:
                return PairedAnalysisEligibility.INELIGIBLE
        return PairedAnalysisEligibility.ELIGIBLE

    def record(self, outcome: CaptureAttemptOutcome) -> CaptureBatchSnapshot:
        for current in self.outcomes:
            if current.attempt_id == outcome.attempt_id:
                if current != outcome:
                    raise ValueError(
                        "capture attempt already has a different terminal outcome"
                    )
                return self
        expected_index = {
            item.attempt_id: index
            for index, item in enumerate(self.definition.expected_attempts)
        }
        if outcome.attempt_id not in expected_index:
            raise ValueError("capture outcome does not belong to this batch")
        outcomes = tuple(
            sorted(
                (*self.outcomes, outcome),
                key=lambda item: expected_index[item.attempt_id],
            )
        )
        return replace(self, outcomes=outcomes, revision=self.revision + 1)


@dataclass(frozen=True)
class AdmittedCapture:
    attempt_id: CaptureAttemptId
    radio_id: RadioId
    plan_id: PlanId
    recording_ref: PublishedRecordingRef


@dataclass(frozen=True)
class PairedCaptureAdmission:
    """Exact pair authorized for downstream analysis, never a latest lookup.

    Independent mode binds two successful recordings for comparison but makes
    no synchronization claim.  Coordinated mode additionally proves that the
    measured first-sample start skew met the batch's declared upper bound; it
    does not claim zero skew or hardware-clock synchronization.
    """

    schema: SchemaRef
    batch_id: CaptureBatchId
    mode: CaptureBatchMode
    captures: tuple[AdmittedCapture, AdmittedCapture]
    requested_start_skew_ns: int
    observed_start_skew_ns: int
    maximum_observed_start_skew_ns: int | None

    SCHEMA_ID = "org.leo-flow.paired-capture-admission"

    @property
    def idempotency_key(self) -> str:
        return f"paired-capture:{self.batch_id}"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported paired capture admission schema")
        if not isinstance(self.mode, CaptureBatchMode):
            raise TypeError("paired admission mode must be a CaptureBatchMode")
        if not isinstance(self.captures, tuple) or len(self.captures) != 2:
            raise ValueError("paired admission requires exactly two captures")
        if tuple(sorted(self.captures, key=lambda item: str(item.attempt_id))) != (
            self.captures
        ):
            raise ValueError("admitted captures must use canonical attempt order")
        if (
            len({item.attempt_id for item in self.captures}) != 2
            or len({item.radio_id for item in self.captures}) != 2
            or len({item.plan_id for item in self.captures}) != 2
            or len({item.recording_ref.recording_id for item in self.captures}) != 2
        ):
            raise ValueError("paired admission identities must be unique")
        _require_nonnegative_integer(
            self.requested_start_skew_ns, "requested_start_skew_ns"
        )
        _require_nonnegative_integer(
            self.observed_start_skew_ns, "observed_start_skew_ns"
        )
        if self.mode is CaptureBatchMode.INDEPENDENT:
            if self.maximum_observed_start_skew_ns is not None:
                raise ValueError("independent admission cannot declare a skew limit")
        else:
            maximum = self.maximum_observed_start_skew_ns
            if maximum is None:
                raise ValueError("coordinated admission requires a skew limit")
            _require_nonnegative_integer(maximum, "maximum_observed_start_skew_ns")
            if self.requested_start_skew_ns != 0:
                raise ValueError("coordinated admission requires one requested start")
            if self.observed_start_skew_ns > maximum:
                raise ValueError(
                    "coordinated admission exceeds its observed skew limit"
                )
