"""Versioned read contracts for capture-batch dashboard projections."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ._validation import require_utc_ns
from .capture_batch import (
    CaptureBatchMode,
    PairedAnalysisEligibility,
)
from .core import (
    V0_1,
    CaptureAttemptId,
    CaptureBatchId,
    PlanId,
    RadioId,
    RecordingId,
    SchemaRef,
    UtcNs,
)
from .dashboard import Page

_FAILURE_REASON = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")


def _require_nonnegative_integer(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")


class DashboardCaptureState(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DashboardAnalysisState(str, Enum):
    UNAVAILABLE = "unavailable"
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class CoordinationClaim(str, Enum):
    """The deliberately bounded timing claim shown to operators."""

    NONE = "none"
    MEASURED_SOFTWARE_COORDINATION = "measured_software_coordination"


@dataclass(frozen=True)
class CaptureBatchTimeRangeQuery:
    """Select batches by their earliest requested attempt start."""

    start_utc_ns: UtcNs
    stop_utc_ns: UtcNs

    def __post_init__(self) -> None:
        if self.stop_utc_ns <= self.start_utc_ns:
            raise ValueError("capture batch time range must be non-empty")


@dataclass(frozen=True)
class CaptureAttemptDashboardView:
    attempt_id: CaptureAttemptId
    radio_id: RadioId
    plan_id: PlanId
    requested_start_utc_ns: UtcNs
    capture_state: DashboardCaptureState
    observed_start_utc_ns: UtcNs | None
    recording_id: RecordingId | None
    failure_reason: str | None
    analysis_state: DashboardAnalysisState
    analysis_result_available: bool

    def __post_init__(self) -> None:
        require_utc_ns(self.requested_start_utc_ns, "requested_start_utc_ns")
        if self.observed_start_utc_ns is not None:
            require_utc_ns(self.observed_start_utc_ns, "observed_start_utc_ns")
        if not isinstance(self.capture_state, DashboardCaptureState):
            raise TypeError("capture_state must be a DashboardCaptureState")
        if not isinstance(self.analysis_state, DashboardAnalysisState):
            raise TypeError("analysis_state must be a DashboardAnalysisState")
        if not isinstance(self.analysis_result_available, bool):
            raise TypeError("analysis_result_available must be a boolean")
        if self.capture_state is DashboardCaptureState.PENDING:
            if any(
                value is not None
                for value in (
                    self.observed_start_utc_ns,
                    self.recording_id,
                    self.failure_reason,
                )
            ):
                raise ValueError("pending capture cannot have a terminal outcome")
            if self.analysis_state is not DashboardAnalysisState.UNAVAILABLE:
                raise ValueError("pending capture cannot have an analysis state")
        elif self.capture_state is DashboardCaptureState.FAILED:
            if (
                self.failure_reason is None
                or self.recording_id is not None
                or _FAILURE_REASON.fullmatch(self.failure_reason) is None
            ):
                raise ValueError(
                    "failed capture requires no recording and a bounded reason"
                )
            if self.analysis_state is not DashboardAnalysisState.UNAVAILABLE:
                raise ValueError("failed capture cannot have an analysis state")
        else:
            if (
                self.observed_start_utc_ns is None
                or self.recording_id is None
                or self.failure_reason is not None
            ):
                raise ValueError(
                    "successful capture requires an observed start and recording"
                )
            if self.analysis_state is DashboardAnalysisState.UNAVAILABLE:
                raise ValueError("successful capture requires an analysis state")
        if self.analysis_result_available and (
            self.capture_state is not DashboardCaptureState.SUCCEEDED
            or self.analysis_state is not DashboardAnalysisState.COMPLETE
        ):
            raise ValueError(
                "an analysis result is available only for completed analysis"
            )


@dataclass(frozen=True)
class CaptureBatchDashboardView:
    """Complete operator-facing state for one immutable two-radio batch."""

    schema: SchemaRef
    batch_id: CaptureBatchId
    mode: CaptureBatchMode
    coordination_claim: CoordinationClaim
    attempts: tuple[CaptureAttemptDashboardView, CaptureAttemptDashboardView]
    revision: int
    requested_start_skew_ns: int
    observed_start_skew_ns: int | None
    maximum_observed_start_skew_ns: int | None
    paired_analysis_eligibility: PairedAnalysisEligibility

    SCHEMA_ID = "org.leo-flow.dashboard.capture-batch"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported capture batch dashboard schema")
        if not isinstance(self.mode, CaptureBatchMode):
            raise TypeError("mode must be a CaptureBatchMode")
        if not isinstance(self.coordination_claim, CoordinationClaim):
            raise TypeError("coordination_claim must be a CoordinationClaim")
        if not isinstance(self.paired_analysis_eligibility, PairedAnalysisEligibility):
            raise TypeError(
                "paired_analysis_eligibility must be a PairedAnalysisEligibility"
            )
        if not isinstance(self.attempts, tuple) or len(self.attempts) != 2:
            raise ValueError("capture batch dashboard view requires two attempts")
        if tuple(sorted(self.attempts, key=lambda item: str(item.attempt_id))) != (
            self.attempts
        ):
            raise ValueError("dashboard attempts must use canonical ID order")
        for field, values in (
            ("attempt IDs", tuple(item.attempt_id for item in self.attempts)),
            ("radio IDs", tuple(item.radio_id for item in self.attempts)),
            ("plan IDs", tuple(item.plan_id for item in self.attempts)),
        ):
            if len(set(values)) != 2:
                raise ValueError(f"capture batch dashboard {field} must be unique")
        _require_nonnegative_integer(self.revision, "revision")
        _require_nonnegative_integer(
            self.requested_start_skew_ns, "requested_start_skew_ns"
        )
        if self.observed_start_skew_ns is not None:
            _require_nonnegative_integer(
                self.observed_start_skew_ns, "observed_start_skew_ns"
            )
        if self.maximum_observed_start_skew_ns is not None:
            _require_nonnegative_integer(
                self.maximum_observed_start_skew_ns,
                "maximum_observed_start_skew_ns",
            )
        terminal_count = sum(
            item.capture_state is not DashboardCaptureState.PENDING
            for item in self.attempts
        )
        if self.revision != terminal_count:
            raise ValueError("revision must equal the terminal attempt count")
        expected_requested_skew = abs(
            int(self.attempts[0].requested_start_utc_ns)
            - int(self.attempts[1].requested_start_utc_ns)
        )
        if self.requested_start_skew_ns != expected_requested_skew:
            raise ValueError("requested start skew does not match attempt starts")
        recording_ids = tuple(
            item.recording_id for item in self.attempts if item.recording_id is not None
        )
        if len(recording_ids) != len(set(recording_ids)):
            raise ValueError("successful attempts must expose distinct recordings")
        successful = all(
            item.capture_state is DashboardCaptureState.SUCCEEDED
            for item in self.attempts
        )
        expected_observed_skew = (
            abs(
                int(self.attempts[0].observed_start_utc_ns)
                - int(self.attempts[1].observed_start_utc_ns)
            )
            if self.attempts[0].observed_start_utc_ns is not None
            and self.attempts[1].observed_start_utc_ns is not None
            else None
        )
        if self.observed_start_skew_ns != expected_observed_skew:
            raise ValueError("observed start skew does not match capture outcomes")
        if self.mode is CaptureBatchMode.INDEPENDENT:
            if self.coordination_claim is not CoordinationClaim.NONE:
                raise ValueError("independent capture makes no coordination claim")
            if self.maximum_observed_start_skew_ns is not None:
                raise ValueError("independent capture cannot declare a skew limit")
        else:
            if (
                self.coordination_claim
                is not CoordinationClaim.MEASURED_SOFTWARE_COORDINATION
            ):
                raise ValueError(
                    "coordinated capture has a measured software claim only"
                )
            if (
                self.maximum_observed_start_skew_ns is None
                or self.maximum_observed_start_skew_ns < 0
                or self.requested_start_skew_ns != 0
            ):
                raise ValueError(
                    "coordinated capture requires one start and a skew limit"
                )
        expected_eligibility = PairedAnalysisEligibility.PENDING
        if terminal_count == 2:
            expected_eligibility = PairedAnalysisEligibility.INELIGIBLE
            if successful and (
                self.mode is CaptureBatchMode.INDEPENDENT
                or (
                    self.observed_start_skew_ns is not None
                    and self.maximum_observed_start_skew_ns is not None
                    and self.observed_start_skew_ns
                    <= self.maximum_observed_start_skew_ns
                )
            ):
                expected_eligibility = PairedAnalysisEligibility.ELIGIBLE
        if self.paired_analysis_eligibility is not expected_eligibility:
            raise ValueError("paired-analysis eligibility contradicts batch outcomes")

    @property
    def requested_start_utc_ns(self) -> UtcNs:
        return UtcNs(min(int(item.requested_start_utc_ns) for item in self.attempts))


class CaptureBatchDashboardQueryPortV0_1(Protocol):
    """Read-only port added alongside, never to, the frozen dashboard v1 port."""

    def recent_capture_batches(
        self, query: CaptureBatchTimeRangeQuery, cursor: str | None = None
    ) -> Page[CaptureBatchDashboardView]: ...

    def capture_batch(self, batch_id: CaptureBatchId) -> CaptureBatchDashboardView: ...
