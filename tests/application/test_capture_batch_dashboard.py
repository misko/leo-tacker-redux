from __future__ import annotations

from leo_flow.application.capture_batch_dashboard import (
    CaptureBatchDashboardPublisher,
    initial_capture_batch_dashboard_view,
)
from leo_flow.contracts.capture_batch import (
    CaptureAttemptOutcome,
    CaptureAttemptState,
    CaptureBatchDefinition,
    CaptureBatchMode,
    CaptureBatchSnapshot,
    ExpectedCaptureAttempt,
    PairedAnalysisEligibility,
)
from leo_flow.contracts.core import (
    CaptureAttemptId,
    CaptureBatchId,
    Digest,
    PlanId,
    RadioId,
    RecordingId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.dashboard_batch import (
    CaptureBatchDashboardView,
    CoordinationClaim,
    DashboardAnalysisState,
    DashboardCaptureState,
)
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)


def _definition() -> CaptureBatchDefinition:
    return CaptureBatchDefinition(
        SchemaRef(CaptureBatchDefinition.SCHEMA_ID),
        CaptureBatchId("cbatch_dashboard_mapper"),
        CaptureBatchMode.INDEPENDENT,
        (
            ExpectedCaptureAttempt(
                CaptureAttemptId("cattempt_a"),
                RadioId("radio_a"),
                PlanId("plan_a"),
                UtcNs(1_000),
            ),
            ExpectedCaptureAttempt(
                CaptureAttemptId("cattempt_b"),
                RadioId("radio_b"),
                PlanId("plan_b"),
                UtcNs(2_000),
            ),
        ),
    )


def _recording() -> PublishedRecordingRef:
    data = Digest.sha256(b"dashboard-data")
    metadata = Digest.sha256(b"dashboard-metadata")
    return PublishedRecordingRef(
        RecordingObjectRef(
            RecordingId("rec_dashboard_mapper"),
            ObjectRef(data, 4, "application/octet-stream", "data-v1", "cas:data"),
            ObjectRef(metadata, 8, "application/json", "metadata-v1", "cas:metadata"),
            Digest.sha256(b"dashboard-manifest"),
        )
    )


def _success(definition: CaptureBatchDefinition) -> CaptureAttemptOutcome:
    expected = definition.expected_attempts[0]
    return CaptureAttemptOutcome(
        SchemaRef(CaptureAttemptOutcome.SCHEMA_ID),
        definition.batch_id,
        expected.attempt_id,
        expected.radio_id,
        expected.plan_id,
        CaptureAttemptState.SUCCEEDED,
        UtcNs(2_200),
        UtcNs(2_100),
        _recording(),
    )


def _failure(definition: CaptureBatchDefinition) -> CaptureAttemptOutcome:
    expected = definition.expected_attempts[1]
    return CaptureAttemptOutcome(
        SchemaRef(CaptureAttemptOutcome.SCHEMA_ID),
        definition.batch_id,
        expected.attempt_id,
        expected.radio_id,
        expected.plan_id,
        CaptureAttemptState.FAILED,
        UtcNs(2_300),
        UtcNs(2_050),
        failure_reason="capture_runner_failed",
    )


def test_initial_snapshot_maps_both_attempts_to_unavailable_pending_capture() -> None:
    definition = _definition()
    view = initial_capture_batch_dashboard_view(
        CaptureBatchSnapshot(SchemaRef(CaptureBatchSnapshot.SCHEMA_ID), definition)
    )

    assert [item.attempt_id for item in view.attempts] == [
        CaptureAttemptId("cattempt_a"),
        CaptureAttemptId("cattempt_b"),
    ]
    assert all(
        item.capture_state is DashboardCaptureState.PENDING
        and item.analysis_state is DashboardAnalysisState.UNAVAILABLE
        and not item.analysis_result_available
        for item in view.attempts
    )
    assert view.revision == 0
    assert view.coordination_claim is CoordinationClaim.NONE
    assert view.paired_analysis_eligibility is PairedAnalysisEligibility.PENDING


def test_partial_success_maps_analysis_pending_without_claiming_a_result() -> None:
    definition = _definition()
    snapshot = CaptureBatchSnapshot(
        SchemaRef(CaptureBatchSnapshot.SCHEMA_ID), definition
    ).record(_success(definition))
    view = initial_capture_batch_dashboard_view(snapshot)
    by_id = {item.attempt_id: item for item in view.attempts}

    succeeded = by_id[CaptureAttemptId("cattempt_a")]
    assert succeeded.capture_state is DashboardCaptureState.SUCCEEDED
    assert succeeded.analysis_state is DashboardAnalysisState.PENDING
    assert not succeeded.analysis_result_available
    assert succeeded.recording_id == RecordingId("rec_dashboard_mapper")
    assert by_id[CaptureAttemptId("cattempt_b")].capture_state is (
        DashboardCaptureState.PENDING
    )


def test_terminal_peer_failure_preserves_observed_start_and_is_unavailable() -> None:
    definition = _definition()
    snapshot = (
        CaptureBatchSnapshot(SchemaRef(CaptureBatchSnapshot.SCHEMA_ID), definition)
        .record(_success(definition))
        .record(_failure(definition))
    )
    view = initial_capture_batch_dashboard_view(snapshot)
    by_id = {item.attempt_id: item for item in view.attempts}

    failed = by_id[CaptureAttemptId("cattempt_b")]
    assert failed.capture_state is DashboardCaptureState.FAILED
    assert failed.observed_start_utc_ns == UtcNs(2_050)
    assert failed.analysis_state is DashboardAnalysisState.UNAVAILABLE
    assert view.revision == 2
    assert view.observed_start_skew_ns == 50
    assert view.paired_analysis_eligibility is PairedAnalysisEligibility.INELIGIBLE


def test_application_publisher_replays_the_exact_deterministic_view() -> None:
    class Writer:
        def __init__(self) -> None:
            self.views: list[CaptureBatchDashboardView] = []

        def publish(self, view: CaptureBatchDashboardView) -> int:
            self.views.append(view)
            return 1

    definition = _definition()
    snapshot = (
        CaptureBatchSnapshot(SchemaRef(CaptureBatchSnapshot.SCHEMA_ID), definition)
        .record(_success(definition))
        .record(_failure(definition))
    )
    writer = Writer()
    publisher = CaptureBatchDashboardPublisher(writer)

    assert publisher.publish_initial(snapshot) == publisher.publish_initial(snapshot)
    assert writer.views == [writer.views[0], writer.views[0]]
