from __future__ import annotations

import threading

import pytest

from leo_flow.application.capture_batches import (
    CaptureBatchCoordinator,
    CaptureBatchNotEligible,
    CaptureBatchNotFound,
    InMemoryCaptureBatchStateStore,
)
from leo_flow.capture.dual import (
    CaptureAttemptControl,
    CaptureAttemptFailureReason,
    CaptureAttemptRunnerFailure,
    CaptureAttemptRunResult,
    DualCaptureConfigurationError,
    DualCaptureExecutor,
    UtcCoordinatedReleaseGate,
)
from leo_flow.contracts.capture_batch import (
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


def _definition(mode: CaptureBatchMode) -> CaptureBatchDefinition:
    second_start = 1_000 if mode is CaptureBatchMode.COORDINATED else 1_100
    return CaptureBatchDefinition(
        SchemaRef(CaptureBatchDefinition.SCHEMA_ID),
        CaptureBatchId(f"cbatch_{mode.value}"),
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
                UtcNs(second_start),
            ),
        ),
        10 if mode is CaptureBatchMode.COORDINATED else None,
    )


def _recording(suffix: str) -> PublishedRecordingRef:
    data = Digest.sha256(f"{suffix}:data".encode())
    metadata = Digest.sha256(f"{suffix}:metadata".encode())
    return PublishedRecordingRef(
        RecordingObjectRef(
            RecordingId(f"rec_dual_{suffix}"),
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


class _Runner:
    def __init__(
        self,
        batch_id: CaptureBatchId,
        *,
        observed_start_utc_ns: int,
        concurrent_barrier: threading.Barrier | None = None,
        allow_ready: threading.Event | None = None,
        fail: bool = False,
        hang_after_release: bool = False,
        mismatched_radio: bool = False,
    ) -> None:
        self.batch_id = batch_id
        self.observed_start_utc_ns = observed_start_utc_ns
        self.concurrent_barrier = concurrent_barrier
        self.allow_ready = allow_ready
        self.fail = fail
        self.hang_after_release = hang_after_release
        self.mismatched_radio = mismatched_radio
        self.entered = threading.Event()
        self.released = threading.Event()
        self.exited = threading.Event()

    def run(
        self, attempt: ExpectedCaptureAttempt, control: CaptureAttemptControl
    ) -> CaptureAttemptRunResult:
        self.entered.set()
        try:
            while self.allow_ready is not None and not self.allow_ready.wait(0.005):
                if control.cancelled:
                    raise RuntimeError("cancelled before ready")
            if not control.ready_and_wait_for_release():
                raise RuntimeError("cancelled at start gate")
            self.released.set()
            if self.concurrent_barrier is not None:
                self.concurrent_barrier.wait(1)
            if self.hang_after_release:
                while not control.cancelled:
                    threading.Event().wait(0.005)
                raise RuntimeError("cancelled after finish timeout")
            if self.fail:
                raise RuntimeError("untrusted runner failure text")
            radio_id = (
                RadioId("radio_wrong") if self.mismatched_radio else attempt.radio_id
            )
            return CaptureAttemptRunResult(
                SchemaRef(CaptureAttemptRunResult.SCHEMA_ID),
                self.batch_id,
                attempt.attempt_id,
                radio_id,
                attempt.plan_id,
                UtcNs(self.observed_start_utc_ns),
                UtcNs(self.observed_start_utc_ns + 50),
                _recording(str(attempt.attempt_id)),
            )
        finally:
            self.exited.set()


class _ObservingRecorder:
    def __init__(self, coordinator: CaptureBatchCoordinator) -> None:
        self.coordinator = coordinator
        self.recorded: list[CaptureBatchSnapshot] = []

    def register(self, definition: CaptureBatchDefinition) -> CaptureBatchSnapshot:
        return self.coordinator.register(definition)

    def record(self, outcome) -> CaptureBatchSnapshot:
        state = self.coordinator.record(outcome)
        self.recorded.append(state)
        if not state.terminal:
            assert (
                state.paired_analysis_eligibility is PairedAnalysisEligibility.PENDING
            )
            with pytest.raises(CaptureBatchNotEligible, match="pending"):
                self.coordinator.admit_paired_analysis(state.batch_id)
        return state


def _executor(recorder) -> DualCaptureExecutor:
    return DualCaptureExecutor(
        recorder,
        startup_timeout_s=0.1,
        finish_timeout_s=0.1,
        cleanup_timeout_s=0.1,
        now_utc_ns=lambda: 9_000,
    )


def _runners(
    definition: CaptureBatchDefinition, first: _Runner, second: _Runner
) -> dict[RadioId, _Runner]:
    return {
        definition.expected_attempts[0].radio_id: first,
        definition.expected_attempts[1].radio_id: second,
    }


def test_independent_attempts_run_concurrently_and_admit_only_after_both() -> None:
    definition = _definition(CaptureBatchMode.INDEPENDENT)
    barrier = threading.Barrier(2)
    first = _Runner(
        definition.batch_id,
        observed_start_utc_ns=1_010,
        concurrent_barrier=barrier,
    )
    second = _Runner(
        definition.batch_id,
        observed_start_utc_ns=5_000,
        concurrent_barrier=barrier,
    )
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())
    recorder = _ObservingRecorder(coordinator)

    state = _executor(recorder).execute(definition, _runners(definition, first, second))

    assert state.terminal
    assert state.paired_analysis_eligibility is PairedAnalysisEligibility.ELIGIBLE
    assert len(recorder.recorded) == 2
    assert not recorder.recorded[0].terminal
    assert recorder.recorded[1].terminal
    assert first.exited.is_set() and second.exited.is_set()


def test_coordinated_attempts_wait_for_one_common_software_release() -> None:
    definition = _definition(CaptureBatchMode.COORDINATED)
    allow_second_ready = threading.Event()
    first = _Runner(definition.batch_id, observed_start_utc_ns=1_000)
    second = _Runner(
        definition.batch_id,
        observed_start_utc_ns=1_007,
        allow_ready=allow_second_ready,
    )
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())
    results: list[CaptureBatchSnapshot] = []

    execution = threading.Thread(
        target=lambda: results.append(
            _executor(coordinator).execute(
                definition, _runners(definition, first, second)
            )
        )
    )
    execution.start()
    assert first.entered.wait(1)
    assert second.entered.wait(1)
    assert not first.released.wait(0.03)
    allow_second_ready.set()
    execution.join(1)

    assert not execution.is_alive()
    assert first.released.is_set() and second.released.is_set()
    assert results[0].observed_start_skew_ns == 7
    assert results[0].paired_analysis_eligibility is PairedAnalysisEligibility.ELIGIBLE


def test_utc_release_gate_waits_until_requested_time_without_early_release() -> None:
    now = [900]
    delays: list[float] = []

    def delay(seconds: float) -> None:
        delays.append(seconds)
        now[0] += round(seconds * 1e9)

    gate = UtcCoordinatedReleaseGate(
        20, now_utc_ns=lambda: now[0], delay=delay, poll_interval_s=0.00000005
    )

    assert gate.admit(UtcNs(1_000), cancelled=lambda: False)
    assert now[0] == 1_000
    assert delays


def test_coordinated_release_lateness_rejects_both_before_capture() -> None:
    definition = _definition(CaptureBatchMode.COORDINATED)
    first = _Runner(definition.batch_id, observed_start_utc_ns=1_000)
    second = _Runner(definition.batch_id, observed_start_utc_ns=1_001)
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())
    executor = DualCaptureExecutor(
        coordinator,
        startup_timeout_s=0.1,
        finish_timeout_s=0.1,
        cleanup_timeout_s=0.1,
        now_utc_ns=lambda: 2_000,
        coordinated_release_admission=UtcCoordinatedReleaseGate(
            10, now_utc_ns=lambda: 1_011
        ),
    )

    state = executor.execute(definition, _runners(definition, first, second))

    assert tuple(item.failure_reason for item in state.outcomes) == (
        "capture_release_window_missed",
        "capture_release_window_missed",
    )
    assert not first.released.is_set() and not second.released.is_set()


def test_expired_absolute_deadline_records_both_without_starting_runners() -> None:
    definition = _definition(CaptureBatchMode.COORDINATED)
    first = _Runner(definition.batch_id, observed_start_utc_ns=1_000)
    second = _Runner(definition.batch_id, observed_start_utc_ns=1_001)
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())

    state = _executor(coordinator).execute(
        definition,
        _runners(definition, first, second),
        deadline_utc_ns=UtcNs(8_999),
    )

    assert tuple(item.failure_reason for item in state.outcomes) == (
        "capture_slot_deadline",
        "capture_slot_deadline",
    )
    assert not first.entered.is_set() and not second.entered.is_set()


def test_absolute_deadline_caps_finish_and_cancels_both_runners() -> None:
    definition = _definition(CaptureBatchMode.COORDINATED)
    first = _Runner(
        definition.batch_id,
        observed_start_utc_ns=1_000,
        hang_after_release=True,
    )
    second = _Runner(
        definition.batch_id,
        observed_start_utc_ns=1_001,
        hang_after_release=True,
    )
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())
    executor = DualCaptureExecutor(
        coordinator,
        startup_timeout_s=1,
        finish_timeout_s=1,
        cleanup_timeout_s=0.1,
        now_utc_ns=lambda: 0,
    )

    state = executor.execute(
        definition,
        _runners(definition, first, second),
        deadline_utc_ns=UtcNs(10_000_000),
    )

    assert tuple(item.failure_reason for item in state.outcomes) == (
        "capture_slot_deadline",
        "capture_slot_deadline",
    )
    assert first.exited.is_set() and second.exited.is_set()


def test_coordinated_eligibility_uses_runner_first_sample_evidence() -> None:
    definition = _definition(CaptureBatchMode.COORDINATED)
    first = _Runner(definition.batch_id, observed_start_utc_ns=1_000)
    second = _Runner(definition.batch_id, observed_start_utc_ns=1_011)
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())

    state = _executor(coordinator).execute(
        definition, _runners(definition, first, second)
    )

    assert state.observed_start_skew_ns == 11
    assert state.paired_analysis_eligibility is PairedAnalysisEligibility.INELIGIBLE


def test_one_side_failure_keeps_successful_solo_recording() -> None:
    definition = _definition(CaptureBatchMode.INDEPENDENT)
    first = _Runner(definition.batch_id, observed_start_utc_ns=1_000)
    second = _Runner(definition.batch_id, observed_start_utc_ns=1_010, fail=True)
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())

    state = _executor(coordinator).execute(
        definition, _runners(definition, first, second)
    )

    assert tuple(item.state for item in state.outcomes) == (
        CaptureAttemptState.SUCCEEDED,
        CaptureAttemptState.FAILED,
    )
    assert len(state.successful_recordings) == 1
    assert state.outcomes[1].failure_reason == "capture_runner_failed"
    assert state.paired_analysis_eligibility is PairedAnalysisEligibility.INELIGIBLE


class _PhaseFailureRunner:
    def run(
        self, _attempt: ExpectedCaptureAttempt, _control: CaptureAttemptControl
    ) -> CaptureAttemptRunResult:
        raise CaptureAttemptRunnerFailure(
            CaptureAttemptFailureReason.FIRST_SEGMENT_CONFIGURATION
        )


def test_coordinated_pre_ready_runner_reason_is_preserved_for_failed_side() -> None:
    definition = _definition(CaptureBatchMode.COORDINATED)
    first = _Runner(definition.batch_id, observed_start_utc_ns=1_000)
    second = _PhaseFailureRunner()
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())

    state = _executor(coordinator).execute(
        definition,
        {
            definition.expected_attempts[0].radio_id: first,
            definition.expected_attempts[1].radio_id: second,
        },
    )

    assert tuple(item.failure_reason for item in state.outcomes) == (
        "capture_peer_startup_failed",
        "capture_first_segment_configuration_failed",
    )
    assert not first.released.is_set()


def test_runner_failure_rejects_non_fixed_reason() -> None:
    with pytest.raises(TypeError, match="fixed reason code"):
        CaptureAttemptRunnerFailure("untrusted raw detail")  # type: ignore[arg-type]


def test_startup_timeout_cancels_stalled_side_and_preserves_peer() -> None:
    definition = _definition(CaptureBatchMode.INDEPENDENT)
    first = _Runner(definition.batch_id, observed_start_utc_ns=1_000)
    never_ready = threading.Event()
    second = _Runner(
        definition.batch_id,
        observed_start_utc_ns=1_010,
        allow_ready=never_ready,
    )
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())
    executor = DualCaptureExecutor(
        coordinator,
        startup_timeout_s=0.03,
        finish_timeout_s=0.1,
        cleanup_timeout_s=0.1,
        now_utc_ns=lambda: 9_000,
    )

    state = executor.execute(definition, _runners(definition, first, second))

    assert state.outcomes[0].state is CaptureAttemptState.SUCCEEDED
    assert state.outcomes[1].failure_reason == "capture_startup_timeout"
    assert first.exited.is_set() and second.exited.is_set()


def test_coordinated_startup_timeout_cancels_both_without_releasing_capture() -> None:
    definition = _definition(CaptureBatchMode.COORDINATED)
    first = _Runner(definition.batch_id, observed_start_utc_ns=1_000)
    never_ready = threading.Event()
    second = _Runner(
        definition.batch_id,
        observed_start_utc_ns=1_005,
        allow_ready=never_ready,
    )
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())
    executor = DualCaptureExecutor(
        coordinator,
        startup_timeout_s=0.03,
        finish_timeout_s=0.1,
        cleanup_timeout_s=0.1,
        now_utc_ns=lambda: 9_000,
    )

    state = executor.execute(definition, _runners(definition, first, second))

    assert tuple(item.failure_reason for item in state.outcomes) == (
        "capture_peer_startup_timeout",
        "capture_startup_timeout",
    )
    assert state.successful_recordings == ()
    assert not first.released.is_set() and not second.released.is_set()
    assert first.exited.is_set() and second.exited.is_set()


def test_finish_timeout_cancels_runner_and_records_both_terminal_outcomes() -> None:
    definition = _definition(CaptureBatchMode.INDEPENDENT)
    first = _Runner(definition.batch_id, observed_start_utc_ns=1_000)
    second = _Runner(
        definition.batch_id,
        observed_start_utc_ns=1_010,
        hang_after_release=True,
    )
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())
    executor = DualCaptureExecutor(
        coordinator,
        startup_timeout_s=0.1,
        finish_timeout_s=0.03,
        cleanup_timeout_s=0.1,
        now_utc_ns=lambda: 9_000,
    )

    state = executor.execute(definition, _runners(definition, first, second))

    assert state.outcomes[0].state is CaptureAttemptState.SUCCEEDED
    assert state.outcomes[1].failure_reason == "capture_finish_timeout"
    assert first.exited.is_set() and second.exited.is_set()


def test_mismatched_runner_identity_fails_closed_without_losing_peer() -> None:
    definition = _definition(CaptureBatchMode.INDEPENDENT)
    first = _Runner(definition.batch_id, observed_start_utc_ns=1_000)
    second = _Runner(
        definition.batch_id,
        observed_start_utc_ns=1_010,
        mismatched_radio=True,
    )
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())

    state = _executor(coordinator).execute(
        definition, _runners(definition, first, second)
    )

    assert state.outcomes[0].state is CaptureAttemptState.SUCCEEDED
    assert state.outcomes[1].failure_reason == "capture_identity_mismatch"
    assert len(state.successful_recordings) == 1


def test_runner_routing_must_exactly_cover_both_expected_radios() -> None:
    definition = _definition(CaptureBatchMode.INDEPENDENT)
    coordinator = CaptureBatchCoordinator(InMemoryCaptureBatchStateStore())
    only_first = {
        definition.expected_attempts[0].radio_id: _Runner(
            definition.batch_id, observed_start_utc_ns=1_000
        )
    }

    with pytest.raises(DualCaptureConfigurationError, match="exactly one runner"):
        _executor(coordinator).execute(definition, only_first)
    with pytest.raises(CaptureBatchNotFound):
        coordinator.inspect(definition.batch_id)
