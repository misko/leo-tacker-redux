from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.contracts.capture_batch import (
    CaptureBatchMode,
    PairedAnalysisEligibility,
)
from leo_flow.contracts.core import (
    V0_1,
    CaptureAttemptId,
    CaptureBatchId,
    PlanId,
    RadioId,
    RecordingId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.dashboard_batch import (
    CaptureAttemptDashboardView,
    CaptureBatchDashboardView,
    CoordinationClaim,
    DashboardAnalysisState,
    DashboardCaptureState,
)


def _attempt(
    suffix: str,
    *,
    state: DashboardCaptureState = DashboardCaptureState.SUCCEEDED,
    observed: int | None = None,
) -> CaptureAttemptDashboardView:
    succeeded = state is DashboardCaptureState.SUCCEEDED
    failed = state is DashboardCaptureState.FAILED
    return CaptureAttemptDashboardView(
        CaptureAttemptId(f"cattempt_{suffix}"),
        RadioId(f"radio_{suffix}"),
        PlanId(f"plan_{suffix}"),
        UtcNs(100),
        state,
        UtcNs(observed) if observed is not None else None,
        RecordingId(f"rec_{suffix}") if succeeded else None,
        "radio_unreachable" if failed else None,
        DashboardAnalysisState.COMPLETE
        if succeeded
        else DashboardAnalysisState.UNAVAILABLE,
        succeeded,
    )


def _view(
    mode: CaptureBatchMode,
    attempts: tuple[CaptureAttemptDashboardView, CaptureAttemptDashboardView],
    *,
    observed_skew: int | None,
    maximum_skew: int | None,
    eligibility: PairedAnalysisEligibility,
) -> CaptureBatchDashboardView:
    return CaptureBatchDashboardView(
        SchemaRef(CaptureBatchDashboardView.SCHEMA_ID, V0_1),
        CaptureBatchId("cbatch_contract"),
        mode,
        CoordinationClaim.NONE
        if mode is CaptureBatchMode.INDEPENDENT
        else CoordinationClaim.MEASURED_SOFTWARE_COORDINATION,
        attempts,
        sum(
            attempt.capture_state is not DashboardCaptureState.PENDING
            for attempt in attempts
        ),
        0,
        observed_skew,
        maximum_skew,
        eligibility,
    )


def test_views_make_only_the_mode_appropriate_coordination_claim() -> None:
    attempts = (_attempt("a", observed=102), _attempt("b", observed=107))
    coordinated = _view(
        CaptureBatchMode.COORDINATED,
        attempts,
        observed_skew=5,
        maximum_skew=10,
        eligibility=PairedAnalysisEligibility.ELIGIBLE,
    )
    independent = _view(
        CaptureBatchMode.INDEPENDENT,
        attempts,
        observed_skew=5,
        maximum_skew=None,
        eligibility=PairedAnalysisEligibility.ELIGIBLE,
    )
    assert (
        coordinated.coordination_claim
        is CoordinationClaim.MEASURED_SOFTWARE_COORDINATION
    )
    assert independent.coordination_claim is CoordinationClaim.NONE
    with pytest.raises(ValueError, match="software claim"):
        replace(coordinated, coordination_claim=CoordinationClaim.NONE)
    with pytest.raises(ValueError, match="no coordination claim"):
        replace(
            independent,
            coordination_claim=CoordinationClaim.MEASURED_SOFTWARE_COORDINATION,
        )


def test_failed_after_sampling_preserves_timing_but_remains_ineligible() -> None:
    attempts = (
        _attempt("a", observed=102),
        _attempt("b", state=DashboardCaptureState.FAILED, observed=124),
    )
    view = _view(
        CaptureBatchMode.INDEPENDENT,
        attempts,
        observed_skew=22,
        maximum_skew=None,
        eligibility=PairedAnalysisEligibility.INELIGIBLE,
    )
    assert view.attempts[1].observed_start_utc_ns == 124
    assert view.observed_start_skew_ns == 22
    assert view.attempts[0].analysis_result_available
    assert view.paired_analysis_eligibility is PairedAnalysisEligibility.INELIGIBLE


def test_batch_view_rejects_inferred_timing_or_analysis_claims() -> None:
    attempts = (_attempt("a", observed=102), _attempt("b", observed=152))
    excessive_skew = _view(
        CaptureBatchMode.COORDINATED,
        attempts,
        observed_skew=50,
        maximum_skew=10,
        eligibility=PairedAnalysisEligibility.INELIGIBLE,
    )
    with pytest.raises(ValueError, match="eligibility contradicts"):
        replace(
            excessive_skew,
            paired_analysis_eligibility=PairedAnalysisEligibility.ELIGIBLE,
        )
    with pytest.raises(ValueError, match="only for completed analysis"):
        replace(
            attempts[0],
            analysis_state=DashboardAnalysisState.PENDING,
            analysis_result_available=True,
        )


def test_batch_view_schema_is_explicitly_v0_1() -> None:
    attempts = (_attempt("a", observed=102), _attempt("b", observed=107))
    view = _view(
        CaptureBatchMode.INDEPENDENT,
        attempts,
        observed_skew=5,
        maximum_skew=None,
        eligibility=PairedAnalysisEligibility.ELIGIBLE,
    )
    with pytest.raises(ValueError, match="unsupported"):
        replace(view, schema=SchemaRef("org.leo-flow.dashboard.other", V0_1))
