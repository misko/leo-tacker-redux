"""Initial dashboard publication for immutable two-radio capture state."""

from __future__ import annotations

from typing import Protocol

from leo_flow.contracts.capture_batch import (
    CaptureAttemptState,
    CaptureBatchMode,
    CaptureBatchSnapshot,
)
from leo_flow.contracts.core import SchemaRef
from leo_flow.contracts.dashboard_batch import (
    CaptureAttemptDashboardView,
    CaptureBatchDashboardView,
    CoordinationClaim,
    DashboardAnalysisState,
    DashboardCaptureState,
)


class CaptureBatchDashboardProjectionWriter(Protocol):
    """Persist one exact public view without exposing storage representation.

    An exact replay must be idempotent. Reusing a batch revision for different
    public content must fail closed.
    """

    def publish(self, view: CaptureBatchDashboardView) -> object: ...


def initial_capture_batch_dashboard_view(
    snapshot: CaptureBatchSnapshot,
) -> CaptureBatchDashboardView:
    """Map capture facts before any local-analysis result exists."""

    outcomes = {item.attempt_id: item for item in snapshot.outcomes}
    attempts: list[CaptureAttemptDashboardView] = []
    for expected in snapshot.definition.expected_attempts:
        outcome = outcomes.get(expected.attempt_id)
        if outcome is None:
            attempts.append(
                CaptureAttemptDashboardView(
                    expected.attempt_id,
                    expected.radio_id,
                    expected.plan_id,
                    expected.requested_start_utc_ns,
                    DashboardCaptureState.PENDING,
                    None,
                    None,
                    None,
                    DashboardAnalysisState.UNAVAILABLE,
                    False,
                )
            )
        elif outcome.state is CaptureAttemptState.SUCCEEDED:
            assert outcome.recording_ref is not None
            attempts.append(
                CaptureAttemptDashboardView(
                    expected.attempt_id,
                    expected.radio_id,
                    expected.plan_id,
                    expected.requested_start_utc_ns,
                    DashboardCaptureState.SUCCEEDED,
                    outcome.observed_start_utc_ns,
                    outcome.recording_ref.recording_id,
                    None,
                    DashboardAnalysisState.PENDING,
                    False,
                )
            )
        else:
            attempts.append(
                CaptureAttemptDashboardView(
                    expected.attempt_id,
                    expected.radio_id,
                    expected.plan_id,
                    expected.requested_start_utc_ns,
                    DashboardCaptureState.FAILED,
                    outcome.observed_start_utc_ns,
                    None,
                    outcome.failure_reason,
                    DashboardAnalysisState.UNAVAILABLE,
                    False,
                )
            )
    canonical_attempts = tuple(sorted(attempts, key=lambda item: str(item.attempt_id)))
    first, second = canonical_attempts
    return CaptureBatchDashboardView(
        SchemaRef(CaptureBatchDashboardView.SCHEMA_ID),
        snapshot.batch_id,
        snapshot.definition.mode,
        CoordinationClaim.NONE
        if snapshot.definition.mode is CaptureBatchMode.INDEPENDENT
        else CoordinationClaim.MEASURED_SOFTWARE_COORDINATION,
        (first, second),
        snapshot.revision,
        snapshot.requested_start_skew_ns,
        snapshot.observed_start_skew_ns,
        snapshot.definition.maximum_observed_start_skew_ns,
        snapshot.paired_analysis_eligibility,
    )


class CaptureBatchDashboardPublisher:
    """Publish the deterministic initial view through a narrow writer port."""

    def __init__(self, writer: CaptureBatchDashboardProjectionWriter) -> None:
        self._writer = writer

    def publish_initial(
        self, snapshot: CaptureBatchSnapshot
    ) -> CaptureBatchDashboardView:
        view = initial_capture_batch_dashboard_view(snapshot)
        self._writer.publish(view)
        return view
