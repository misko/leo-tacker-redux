from __future__ import annotations

import multiprocessing
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from leo_flow.adapters.capture_batch_sqlite import SQLiteCaptureBatchStateStore
from leo_flow.application.capture_batches import CaptureBatchCoordinator
from leo_flow.capture.dual import (
    CaptureAttemptFailureReason,
    CaptureAttemptRunnerFailure,
    CaptureAttemptRunResult,
    DualCaptureExecutor,
)
from leo_flow.capture.v5_station import V5CaptureStation
from leo_flow.contracts.capture_batch import (
    CaptureAttemptState,
    CaptureBatchDefinition,
    CaptureBatchMode,
    ExpectedCaptureAttempt,
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
from leo_flow.deployments import process_isolated_capture, v5_dual_capture_operator
from leo_flow.deployments.process_isolated_capture import (
    IsolatedAttemptPhaseFailure,
    IsolatedAttemptWork,
    IsolatedAttemptWorkFactory,
    SpawnIsolatedAttemptRunner,
    SpawnProcessSupervisor,
)
from leo_flow.deployments.v5_scan import DEVELOPMENT_STATION

_SECRET = "postgres://capture:must-not-be-in-argv@example.invalid/catalog"


def _definition(mode: CaptureBatchMode) -> CaptureBatchDefinition:
    common = mode is CaptureBatchMode.COORDINATED
    return CaptureBatchDefinition(
        SchemaRef(CaptureBatchDefinition.SCHEMA_ID),
        CaptureBatchId(f"cbatch_spawn_{mode.value}"),
        mode,
        (
            ExpectedCaptureAttempt(
                CaptureAttemptId("cattempt_spawn_a"),
                RadioId("radio_spawn_a"),
                PlanId("plan_spawn_a"),
                UtcNs(1_000),
            ),
            ExpectedCaptureAttempt(
                CaptureAttemptId("cattempt_spawn_b"),
                RadioId("radio_spawn_b"),
                PlanId("plan_spawn_b"),
                UtcNs(1_000 if common else 2_000),
            ),
        ),
        1_000_000 if common else None,
    )


def _recording(suffix: str) -> PublishedRecordingRef:
    data = Digest.sha256(f"{suffix}:data".encode())
    metadata = Digest.sha256(f"{suffix}:metadata".encode())
    return PublishedRecordingRef(
        RecordingObjectRef(
            RecordingId(f"rec_spawn_{suffix}"),
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


@dataclass
class _WorkFactory:
    entered_preflight: Any
    finish_preflight: Any
    entered_capture: Any
    crash: bool = False
    hang: bool = False

    def build(
        self,
        station: V5CaptureStation,
        catalog_credential: str,
        batch_id: CaptureBatchId,
    ) -> IsolatedAttemptWork:
        return _Work(
            station,
            catalog_credential,
            batch_id,
            self.entered_preflight,
            self.finish_preflight,
            self.entered_capture,
            self.crash,
            self.hang,
        )


class _Work:
    def __init__(
        self,
        station: V5CaptureStation,
        credential: str,
        batch_id: CaptureBatchId,
        entered_preflight: Any,
        finish_preflight: Any,
        entered_capture: Any,
        crash: bool,
        hang: bool,
    ) -> None:
        self._station = station
        self._credential = credential
        self._batch_id = batch_id
        self._entered_preflight = entered_preflight
        self._finish_preflight = finish_preflight
        self._entered_capture = entered_capture
        self._crash = crash
        self._hang = hang

    def preflight(self) -> None:
        credential = self._credential.encode()
        if credential in b"\0".join(item.encode() for item in sys.argv):
            raise RuntimeError("credential leaked through sys.argv")
        if credential in Path("/proc/self/cmdline").read_bytes():
            raise RuntimeError("credential leaked through process argv")
        if os.environ.get("LEO_PROCESS_TEST_SECRET") is not None:
            raise RuntimeError("ambient secret reached capture child")
        if Path("/proc/self/fd/1").resolve() != Path(os.devnull):
            raise RuntimeError("capture child stdout is not isolated")
        self._entered_preflight.set()
        if not self._finish_preflight.wait(10.0):
            raise RuntimeError("test preflight release timed out")

    def capture(self, attempt: ExpectedCaptureAttempt) -> CaptureAttemptRunResult:
        self._entered_capture.set()
        if self._crash:
            os._exit(17)
        while self._hang:
            time.sleep(1.0)
        observed = UtcNs(os.getpid())
        return CaptureAttemptRunResult(
            SchemaRef(CaptureAttemptRunResult.SCHEMA_ID),
            self._batch_id,
            attempt.attempt_id,
            attempt.radio_id,
            attempt.plan_id,
            observed,
            UtcNs(int(observed) + 100),
            _recording(str(attempt.radio_id)),
        )

    def close(self, timeout_s: float) -> None:
        assert timeout_s == 10.0


@dataclass
class _FailureWorkFactory:
    stage: str
    private_detail: str

    def build(
        self,
        station: V5CaptureStation,
        catalog_credential: str,
        batch_id: CaptureBatchId,
    ) -> IsolatedAttemptWork:
        if self.stage == "build":
            raise RuntimeError(self.private_detail)
        return _FailureWork(
            station,
            catalog_credential,
            batch_id,
            self.stage,
            self.private_detail,
        )


class _FailureWork:
    def __init__(
        self,
        station: V5CaptureStation,
        credential: str,
        batch_id: CaptureBatchId,
        stage: str,
        private_detail: str,
    ) -> None:
        self._station = station
        self._credential = credential
        self._batch_id = batch_id
        self._stage = stage
        self._private_detail = private_detail

    def preflight(self) -> None:
        if self._stage == "preflight":
            raise RuntimeError(self._private_detail)
        if self._stage == "attestation":
            raise IsolatedAttemptPhaseFailure(
                CaptureAttemptFailureReason.RADIO_ATTESTATION
            )

    def capture(self, attempt: ExpectedCaptureAttempt) -> CaptureAttemptRunResult:
        if self._stage == "capture":
            raise RuntimeError(self._private_detail)
        if self._stage in {"publication", "publication_and_cleanup"}:
            raise IsolatedAttemptPhaseFailure(
                CaptureAttemptFailureReason.RECORDING_PUBLICATION
            )
        observed = UtcNs(os.getpid())
        return CaptureAttemptRunResult(
            SchemaRef(CaptureAttemptRunResult.SCHEMA_ID),
            self._batch_id,
            attempt.attempt_id,
            attempt.radio_id,
            attempt.plan_id,
            observed,
            UtcNs(int(observed) + 100),
            _recording(str(attempt.radio_id)),
        )

    def close(self, timeout_s: float) -> None:
        assert timeout_s == 10.0
        if self._stage in {"cleanup", "publication_and_cleanup"}:
            raise RuntimeError(self._private_detail)


def _runner(
    context: Any,
    definition: CaptureBatchDefinition,
    attempt: ExpectedCaptureAttempt,
    factory: IsolatedAttemptWorkFactory,
    *,
    fast_cleanup: bool = False,
    supervisor: SpawnProcessSupervisor | None = None,
    poll_interval_s: float = 0.01,
    post_release_dispatch_delay_s: float = 0.0,
    monotonic: Callable[[], float] = time.monotonic,
    delay: Callable[[float], None] = time.sleep,
) -> SpawnIsolatedAttemptRunner:
    return SpawnIsolatedAttemptRunner(
        DEVELOPMENT_STATION,
        _SECRET,
        definition.batch_id,
        factory,
        supervisor=supervisor,
        process_context=context,
        poll_interval_s=poll_interval_s,
        post_release_dispatch_delay_s=post_release_dispatch_delay_s,
        monotonic=monotonic,
        delay=delay,
        cooperative_cleanup_timeout_s=0.05 if fast_cleanup else 0.25,
        terminate_timeout_s=0.2 if fast_cleanup else 2.0,
        kill_timeout_s=0.2 if fast_cleanup else 2.0,
    )


def test_runner_rejects_fork_context_before_process_construction() -> None:
    definition = _definition(CaptureBatchMode.INDEPENDENT)
    context = multiprocessing.get_context("spawn")
    gate = context.Event()

    with pytest.raises(ValueError, match="spawn start method"):
        SpawnIsolatedAttemptRunner(
            DEVELOPMENT_STATION,
            _SECRET,
            definition.batch_id,
            _WorkFactory(gate, gate, gate),
            process_context=multiprocessing.get_context("fork"),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("delay_s", [-1.0, float("inf"), float("nan"), True])
def test_runner_rejects_invalid_post_release_dispatch_delay(
    delay_s: float,
) -> None:
    definition = _definition(CaptureBatchMode.INDEPENDENT)
    context = multiprocessing.get_context("spawn")
    gate = context.Event()

    with pytest.raises(ValueError, match="finite non-negative"):
        SpawnIsolatedAttemptRunner(
            DEVELOPMENT_STATION,
            _SECRET,
            definition.batch_id,
            _WorkFactory(gate, gate, gate),
            process_context=context,
            post_release_dispatch_delay_s=delay_s,
        )


def test_production_runner_builder_forwards_optional_dispatch_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = _definition(CaptureBatchMode.COORDINATED)
    supervisor = SpawnProcessSupervisor()
    captured: dict[str, object] = {}
    sentinel = object()

    def build_stub(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(
        v5_dual_capture_operator, "SpawnIsolatedAttemptRunner", build_stub
    )

    result = v5_dual_capture_operator._build_process_isolated_runner(
        DEVELOPMENT_STATION,
        _SECRET,
        definition.batch_id,
        supervisor,
        post_release_dispatch_delay_s=0.01,
    )

    assert result is sentinel
    assert captured["kwargs"] == {
        "supervisor": supervisor,
        "post_release_dispatch_delay_s": 0.01,
    }


@pytest.mark.parametrize("mode", list(CaptureBatchMode))
def test_spawn_children_have_distinct_pids_and_preserve_release_semantics(
    tmp_path: Path, mode: CaptureBatchMode, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEO_PROCESS_TEST_SECRET", "must-not-reach-child")
    context = multiprocessing.get_context("spawn")
    definition = _definition(mode)
    first_attempt, second_attempt = definition.expected_attempts
    first_entered, second_entered = context.Event(), context.Event()
    first_release, second_release = context.Event(), context.Event()
    first_capture, second_capture = context.Event(), context.Event()
    first = _runner(
        context,
        definition,
        first_attempt,
        _WorkFactory(first_entered, first_release, first_capture),
    )
    second = _runner(
        context,
        definition,
        second_attempt,
        _WorkFactory(second_entered, second_release, second_capture),
    )
    result: list[object] = []

    def execute() -> None:
        result.append(
            DualCaptureExecutor(
                CaptureBatchCoordinator(
                    SQLiteCaptureBatchStateStore(tmp_path / "batch.sqlite3")
                ),
                startup_timeout_s=10.0,
                finish_timeout_s=10.0,
                cleanup_timeout_s=2.0,
            ).execute(
                definition,
                {
                    first_attempt.radio_id: first,
                    second_attempt.radio_id: second,
                },
            )
        )

    thread = threading.Thread(target=execute)
    thread.start()
    assert first_entered.wait(5.0)
    assert second_entered.wait(5.0)
    first_release.set()
    if mode is CaptureBatchMode.INDEPENDENT:
        assert first_capture.wait(5.0)
    else:
        assert not first_capture.wait(0.2)
    second_release.set()
    thread.join(10.0)

    assert not thread.is_alive()
    assert len(result) == 1
    state = result[0]
    assert all(item.state is CaptureAttemptState.SUCCEEDED for item in state.outcomes)
    assert first_capture.is_set() and second_capture.is_set()
    assert first.last_child_pid not in (None, os.getpid())
    assert second.last_child_pid not in (None, os.getpid(), first.last_child_pid)
    assert [item.recording_ref for item in state.outcomes] == [
        _recording(str(first_attempt.radio_id)),
        _recording(str(second_attempt.radio_id)),
    ]


@pytest.mark.parametrize(("crash", "hang"), ((True, False), (False, True)))
def test_spawn_crash_and_timeout_are_sanitized_and_children_are_reaped(
    tmp_path: Path, crash: bool, hang: bool
) -> None:
    context = multiprocessing.get_context("spawn")
    definition = _definition(CaptureBatchMode.INDEPENDENT)
    first_attempt, second_attempt = definition.expected_attempts
    first_gate = context.Event()
    first_gate.set()
    second_gate = context.Event()
    second_gate.set()
    first = _runner(
        context,
        definition,
        first_attempt,
        _WorkFactory(
            context.Event(), first_gate, context.Event(), crash=crash, hang=hang
        ),
        fast_cleanup=True,
    )
    second = _runner(
        context,
        definition,
        second_attempt,
        _WorkFactory(context.Event(), second_gate, context.Event()),
        fast_cleanup=True,
    )

    state = DualCaptureExecutor(
        CaptureBatchCoordinator(
            SQLiteCaptureBatchStateStore(tmp_path / "batch.sqlite3")
        ),
        startup_timeout_s=5.0,
        finish_timeout_s=0.3 if hang else 5.0,
        cleanup_timeout_s=2.0,
    ).execute(
        definition,
        {first_attempt.radio_id: first, second_attempt.radio_id: second},
    )

    assert [item.state for item in state.outcomes] == [
        CaptureAttemptState.FAILED,
        CaptureAttemptState.SUCCEEDED,
    ]
    assert state.outcomes[0].failure_reason in {
        "capture_runner_failed",
        "capture_finish_timeout",
    }
    assert first.last_child_pid is not None
    assert second.last_child_pid is not None
    assert not _pid_exists(first.last_child_pid)
    assert not _pid_exists(second.last_child_pid)


@pytest.mark.parametrize(
    ("stage", "expected"),
    (
        ("build", CaptureAttemptFailureReason.CHILD_BUILD),
        ("preflight", CaptureAttemptFailureReason.CYCLE_PREFLIGHT),
        ("attestation", CaptureAttemptFailureReason.RADIO_ATTESTATION),
        ("capture", CaptureAttemptFailureReason.CAPTURE_ENGINE),
        ("publication", CaptureAttemptFailureReason.RECORDING_PUBLICATION),
        ("cleanup", CaptureAttemptFailureReason.CHILD_CLEANUP),
        ("publication_and_cleanup", CaptureAttemptFailureReason.CHILD_CLEANUP),
    ),
)
def test_spawn_failure_reports_only_fixed_phase_and_cleanup_takes_precedence(
    stage: str, expected: CaptureAttemptFailureReason
) -> None:
    context = multiprocessing.get_context("spawn")
    definition = _definition(CaptureBatchMode.INDEPENDENT)
    attempt = definition.expected_attempts[0]
    private_detail = "private path /secret and driver value=987"
    runner = _runner(
        context,
        definition,
        attempt,
        _FailureWorkFactory(stage, private_detail),
        fast_cleanup=True,
    )

    with pytest.raises(CaptureAttemptRunnerFailure) as raised:
        runner.run(attempt, _ImmediateControl())

    assert raised.value.reason is expected
    assert str(raised.value) == "capture attempt runner failed"
    assert private_detail not in str(raised.value)


def test_parent_rejects_unrecognized_child_failure_code() -> None:
    failure = process_isolated_capture._runner_failure_from_message(
        ("failed", "private_unvalidated_reason"), "isolated capture child failed"
    )

    assert type(failure) is RuntimeError
    assert str(failure) == "isolated capture child failed"


class _ImmediateControl:
    cancelled = False

    def ready_and_wait_for_release(self) -> bool:
        return True


class _ManualReleaseControl:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.release = threading.Event()
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._cancelled.set()

    def ready_and_wait_for_release(self) -> bool:
        self.ready.set()
        while not self.release.wait(0.01):
            if self.cancelled:
                return False
        return not self.cancelled


def test_post_release_dispatch_delay_follows_ready_and_common_release() -> None:
    context = multiprocessing.get_context("spawn")
    definition = _definition(CaptureBatchMode.COORDINATED)
    attempt = definition.expected_attempts[0]
    entered_preflight = context.Event()
    finish_preflight = context.Event()
    entered_capture = context.Event()
    clock = [17.0]
    delays: list[float] = []
    control = _ManualReleaseControl()

    def fake_delay(seconds: float) -> None:
        assert control.ready.is_set()
        assert control.release.is_set()
        delays.append(seconds)
        clock[0] += seconds

    runner = _runner(
        context,
        definition,
        attempt,
        _WorkFactory(entered_preflight, finish_preflight, entered_capture),
        poll_interval_s=1.0,
        post_release_dispatch_delay_s=0.125,
        monotonic=lambda: clock[0],
        delay=fake_delay,
    )
    results: list[CaptureAttemptRunResult] = []

    thread = threading.Thread(
        target=lambda: results.append(runner.run(attempt, control))
    )
    thread.start()
    assert entered_preflight.wait(5.0)
    finish_preflight.set()
    assert control.ready.wait(5.0)
    assert delays == []
    assert not entered_capture.is_set()

    control.release.set()
    thread.join(5.0)

    assert not thread.is_alive()
    assert len(results) == 1
    assert delays == [0.125]
    assert entered_capture.is_set()


def test_cancellation_during_dispatch_delay_withholds_child_release() -> None:
    context = multiprocessing.get_context("spawn")
    definition = _definition(CaptureBatchMode.COORDINATED)
    attempt = definition.expected_attempts[0]
    preflight_gate = context.Event()
    preflight_gate.set()
    entered_capture = context.Event()
    clock = [41.0]
    control = _ManualReleaseControl()
    delay_entered = threading.Event()

    def cancelling_delay(seconds: float) -> None:
        clock[0] += seconds
        control.cancel()
        delay_entered.set()

    runner = _runner(
        context,
        definition,
        attempt,
        _WorkFactory(context.Event(), preflight_gate, entered_capture),
        fast_cleanup=True,
        poll_interval_s=0.05,
        post_release_dispatch_delay_s=0.125,
        monotonic=lambda: clock[0],
        delay=cancelling_delay,
    )
    failures: list[BaseException] = []

    def execute() -> None:
        try:
            runner.run(attempt, control)
        except BaseException as error:  # noqa: BLE001 - test observes cancellation
            failures.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    assert control.ready.wait(5.0)
    control.release.set()
    assert delay_entered.wait(5.0)
    thread.join(5.0)

    assert not thread.is_alive()
    assert [str(item) for item in failures] == [
        "isolated capture cancelled before dispatch"
    ]
    assert not entered_capture.is_set()
    assert runner.last_child_pid is not None
    assert not _pid_exists(runner.last_child_pid)


def test_zero_dispatch_delay_preserves_immediate_release_without_delay_call() -> None:
    context = multiprocessing.get_context("spawn")
    definition = _definition(CaptureBatchMode.INDEPENDENT)
    attempt = definition.expected_attempts[0]
    preflight_gate = context.Event()
    preflight_gate.set()
    entered_capture = context.Event()

    def forbidden_delay(seconds: float) -> None:
        pytest.fail(f"zero dispatch delay unexpectedly slept for {seconds}")

    result = _runner(
        context,
        definition,
        attempt,
        _WorkFactory(context.Event(), preflight_gate, entered_capture),
        delay=forbidden_delay,
    ).run(attempt, _ImmediateControl())

    assert result.attempt_id == attempt.attempt_id
    assert entered_capture.is_set()


def test_parent_supervisor_aborts_and_reaps_active_child() -> None:
    context = multiprocessing.get_context("spawn")
    definition = _definition(CaptureBatchMode.INDEPENDENT)
    attempt = definition.expected_attempts[0]
    preflight_gate = context.Event()
    preflight_gate.set()
    entered_capture = context.Event()
    supervisor = SpawnProcessSupervisor()
    runner = _runner(
        context,
        definition,
        attempt,
        _WorkFactory(context.Event(), preflight_gate, entered_capture, hang=True),
        fast_cleanup=True,
        supervisor=supervisor,
    )
    failures: list[BaseException] = []

    def execute() -> None:
        try:
            runner.run(attempt, _ImmediateControl())
        except BaseException as error:  # noqa: BLE001 - test observes interruption
            failures.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    assert entered_capture.wait(5.0)
    assert supervisor.active_pids == (runner.last_child_pid,)

    supervisor.abort_all()
    thread.join(2.0)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert str(failures[0]) == "isolated capture child failed"
    assert supervisor.active_pids == ()
    assert runner.last_child_pid is not None
    assert not _pid_exists(runner.last_child_pid)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
