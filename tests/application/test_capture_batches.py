from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.application.capture_batches import (
    CaptureBatchCoordinator,
    CaptureBatchIdentityConflict,
    CaptureBatchNotEligible,
    CaptureBatchNotFound,
    CaptureBatchRevisionConflict,
    InMemoryCaptureBatchStateStore,
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
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)


def _definition(
    *,
    mode: CaptureBatchMode = CaptureBatchMode.INDEPENDENT,
    maximum_skew: int = 10,
) -> CaptureBatchDefinition:
    requested_b = 1_000 if mode is CaptureBatchMode.COORDINATED else 2_000
    return CaptureBatchDefinition(
        SchemaRef(CaptureBatchDefinition.SCHEMA_ID),
        CaptureBatchId("cbatch_dual"),
        mode,
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
                UtcNs(requested_b),
            ),
        ),
        maximum_skew if mode is CaptureBatchMode.COORDINATED else None,
    )


def _recording(suffix: str) -> PublishedRecordingRef:
    data = Digest.sha256(f"{suffix}:data".encode())
    metadata = Digest.sha256(f"{suffix}:metadata".encode())
    return PublishedRecordingRef(
        RecordingObjectRef(
            RecordingId(f"rec_{suffix}"),
            ObjectRef(
                data,
                64,
                "application/octet-stream",
                "recording-data-v1",
                f"cas:sha256:{data.value}",
            ),
            ObjectRef(
                metadata,
                128,
                "application/json",
                "recording-metadata-v1",
                f"cas:sha256:{metadata.value}",
            ),
            Digest.sha256(f"{suffix}:manifest".encode()),
        )
    )


def _success(
    definition: CaptureBatchDefinition, index: int, observed: int
) -> CaptureAttemptOutcome:
    expected = definition.expected_attempts[index]
    return CaptureAttemptOutcome(
        SchemaRef(CaptureAttemptOutcome.SCHEMA_ID),
        definition.batch_id,
        expected.attempt_id,
        expected.radio_id,
        expected.plan_id,
        CaptureAttemptState.SUCCEEDED,
        UtcNs(observed + 50),
        UtcNs(observed),
        _recording(str(index)),
    )


def _failure(definition: CaptureBatchDefinition, index: int) -> CaptureAttemptOutcome:
    expected = definition.expected_attempts[index]
    return CaptureAttemptOutcome(
        SchemaRef(CaptureAttemptOutcome.SCHEMA_ID),
        definition.batch_id,
        expected.attempt_id,
        expected.radio_id,
        expected.plan_id,
        CaptureAttemptState.FAILED,
        UtcNs(2_100),
        failure_reason="radio_disconnected",
    )


def test_success_then_peer_failure_retains_solo_and_never_admits_pair() -> None:
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())
    definition = _definition()
    initial = coordinator.register(definition)
    assert coordinator.register(definition) == initial

    success = _success(definition, 0, 1_100)
    first = coordinator.record(success)
    assert first.revision == 1
    assert first.successful_recordings == (success.recording_ref,)
    assert first.paired_analysis_eligibility is PairedAnalysisEligibility.PENDING
    with pytest.raises(CaptureBatchNotEligible, match="pending"):
        coordinator.admit_paired_analysis(definition.batch_id)

    assert coordinator.register(definition) == first
    assert coordinator.record(success) == first
    terminal = coordinator.record(_failure(definition, 1))
    assert terminal.terminal
    assert terminal.successful_recordings == (success.recording_ref,)
    assert terminal.paired_analysis_eligibility is PairedAnalysisEligibility.INELIGIBLE
    with pytest.raises(CaptureBatchNotEligible, match="ineligible"):
        coordinator.admit_paired_analysis(definition.batch_id)


def test_independent_pair_admission_is_exact_and_idempotent() -> None:
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())
    definition = _definition()
    coordinator.register(definition)
    coordinator.record(_success(definition, 1, 8_000))
    terminal = coordinator.record(_success(definition, 0, 1_100))

    assert terminal.observed_start_skew_ns == 6_900
    assert terminal.paired_analysis_eligibility is PairedAnalysisEligibility.ELIGIBLE
    first = coordinator.admit_paired_analysis(definition.batch_id)
    second = coordinator.admit_paired_analysis(definition.batch_id)
    assert first == second
    assert first.requested_start_skew_ns == 1_000
    assert first.observed_start_skew_ns == 6_900
    assert first.maximum_observed_start_skew_ns is None
    assert first.idempotency_key == "paired-capture:cbatch_dual"
    assert tuple(item.radio_id for item in first.captures) == (
        RadioId("radio_a"),
        RadioId("radio_b"),
    )


@pytest.mark.parametrize(
    ("observed_b", "eligibility"),
    (
        (1_010, PairedAnalysisEligibility.ELIGIBLE),
        (1_011, PairedAnalysisEligibility.INELIGIBLE),
    ),
)
def test_coordinated_pair_observed_skew_is_a_hard_admission_gate(
    observed_b: int, eligibility: PairedAnalysisEligibility
) -> None:
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())
    definition = _definition(mode=CaptureBatchMode.COORDINATED, maximum_skew=10)
    coordinator.register(definition)
    coordinator.record(_success(definition, 0, 1_000))
    terminal = coordinator.record(_success(definition, 1, observed_b))

    assert terminal.observed_start_skew_ns == observed_b - 1_000
    assert terminal.paired_analysis_eligibility is eligibility
    if eligibility is PairedAnalysisEligibility.ELIGIBLE:
        admission = coordinator.admit_paired_analysis(definition.batch_id)
        assert admission.maximum_observed_start_skew_ns == 10
    else:
        with pytest.raises(CaptureBatchNotEligible, match="ineligible"):
            coordinator.admit_paired_analysis(definition.batch_id)


def test_attempt_replay_conflict_and_routing_mismatch_fail_closed() -> None:
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())
    definition = _definition()
    coordinator.register(definition)
    outcome = _success(definition, 0, 1_100)
    coordinator.record(outcome)

    with pytest.raises(CaptureBatchIdentityConflict, match="different terminal"):
        coordinator.record(replace(outcome, terminal_utc_ns=UtcNs(1_151)))
    with pytest.raises(CaptureBatchIdentityConflict, match="another radio or plan"):
        coordinator.record(
            replace(
                _success(definition, 1, 2_100),
                radio_id=definition.expected_attempts[0].radio_id,
            )
        )


def test_revision_conflict_is_retried_without_duplicate_transition() -> None:
    class ConflictOnceStore(InMemoryCaptureBatchStateStore):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def compare_and_swap(
            self,
            batch_id: CaptureBatchId,
            expected_revision: int,
            replacement: CaptureBatchSnapshot,
        ) -> CaptureBatchSnapshot:
            self.calls += 1
            if self.calls == 1:
                raise CaptureBatchRevisionConflict("injected concurrent update")
            return super().compare_and_swap(batch_id, expected_revision, replacement)

    store = ConflictOnceStore()
    coordinator = CaptureBatchCoordinator(store)
    definition = _definition()
    coordinator.register(definition)

    state = coordinator.record(_success(definition, 0, 1_100))

    assert state.revision == 1
    assert store.calls == 2
    assert len(state.outcomes) == 1


def test_revision_retry_exhaustion_fails_without_claiming_completion() -> None:
    class AlwaysConflictStore(InMemoryCaptureBatchStateStore):
        def compare_and_swap(
            self,
            batch_id: CaptureBatchId,
            expected_revision: int,
            replacement: CaptureBatchSnapshot,
        ) -> CaptureBatchSnapshot:
            del batch_id, expected_revision, replacement
            raise CaptureBatchRevisionConflict("injected concurrent update")

    store = AlwaysConflictStore()
    coordinator = CaptureBatchCoordinator(store, maximum_revision_retries=2)
    definition = _definition()
    coordinator.register(definition)

    with pytest.raises(CaptureBatchRevisionConflict, match="retry limit"):
        coordinator.record(_success(definition, 0, 1_100))
    assert coordinator.inspect(definition.batch_id).outcomes == ()


def test_unknown_batch_and_conflicting_definition_are_rejected() -> None:
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())
    definition = _definition()
    coordinator.register(definition)
    with pytest.raises(CaptureBatchIdentityConflict, match="another definition"):
        coordinator.register(
            replace(
                definition,
                expected_attempts=(
                    definition.expected_attempts[0],
                    replace(
                        definition.expected_attempts[1],
                        requested_start_utc_ns=UtcNs(2_001),
                    ),
                ),
            )
        )
    with pytest.raises(CaptureBatchNotFound):
        coordinator.inspect(CaptureBatchId("cbatch_missing"))
