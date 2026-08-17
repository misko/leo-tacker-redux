"""Spawn-isolated execution of one private capture attempt.

The parent remains the dual-batch authority.  A fresh interpreter owns the
radio/libiio stack for exactly one attempt and communicates only readiness,
release, and the exact terminal result over a private pipe.  In particular,
the catalog credential is transferred through the spawn pipe, never argv.
"""

from __future__ import annotations

import ctypes
import math
import multiprocessing
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnContext
from multiprocessing.process import BaseProcess
from typing import Protocol

from leo_flow.capture.dual import (
    CaptureAttemptControl,
    CaptureAttemptFailureReason,
    CaptureAttemptRunner,
    CaptureAttemptRunnerFailure,
    CaptureAttemptRunResult,
)
from leo_flow.capture.v5_station import V5CaptureStation
from leo_flow.contracts.capture_batch import ExpectedCaptureAttempt
from leo_flow.contracts.core import CaptureBatchId

_READY = "ready"
_RELEASE = "release"
_CANCEL = "cancel"
_RESULT = "result"
_FAILED = "failed"


class IsolatedAttemptWork(Protocol):
    """Child-owned radio work; implementations never cross process boundaries."""

    def preflight(self) -> None: ...

    def capture(self, attempt: ExpectedCaptureAttempt) -> CaptureAttemptRunResult: ...

    def close(self, timeout_s: float) -> None: ...


class IsolatedAttemptWorkFactory(Protocol):
    """Pickle-safe factory invoked only after the spawn child starts."""

    def build(
        self,
        station: V5CaptureStation,
        catalog_credential: str,
        batch_id: CaptureBatchId,
    ) -> IsolatedAttemptWork: ...


class IsolatedAttemptPhaseFailure(RuntimeError):
    """Child-private failure carrying only one fixed phase code."""

    def __init__(self, reason: CaptureAttemptFailureReason) -> None:
        if not isinstance(reason, CaptureAttemptFailureReason):
            raise TypeError("isolated failure reason must be a fixed reason code")
        super().__init__("isolated capture phase failed")
        self.reason = reason


class SpawnProcessSupervisor:
    """Parent-owned registry used to abort every child before lock release."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handles: set[_ProcessHandle] = set()

    def start(
        self,
        process: BaseProcess,
        connection: Connection,
        *,
        cooperative_timeout_s: float,
        terminate_timeout_s: float,
        kill_timeout_s: float,
    ) -> _ProcessHandle:
        with self._lock:
            process.start()
            handle = _ProcessHandle(
                process,
                connection,
                cooperative_timeout_s,
                terminate_timeout_s,
                kill_timeout_s,
            )
            self._handles.add(handle)
            return handle

    def discard(self, handle: _ProcessHandle) -> None:
        with self._lock:
            self._handles.discard(handle)

    def abort_all(self) -> None:
        """Idempotently stop and reap every registered process."""

        with self._lock:
            handles = tuple(self._handles)
        failed = False
        for handle in handles:
            try:
                handle.cleanup()
            except Exception:  # noqa: BLE001 - finish every sibling cleanup
                failed = True
            else:
                self.discard(handle)
        if failed:
            raise RuntimeError("isolated capture cleanup failed")

    @property
    def active_pids(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(
                process_pid
                for item in self._handles
                if (process_pid := item.pid) is not None and item.is_alive
            )


class _ProcessHandle:
    def __init__(
        self,
        process: BaseProcess,
        connection: Connection,
        cooperative_timeout_s: float,
        terminate_timeout_s: float,
        kill_timeout_s: float,
    ) -> None:
        self._process = process
        self._connection = connection
        self._cooperative_timeout_s = cooperative_timeout_s
        self._terminate_timeout_s = terminate_timeout_s
        self._kill_timeout_s = kill_timeout_s
        self._lock = threading.Lock()
        self._closed = False

    @property
    def pid(self) -> int | None:
        return self._process.pid

    @property
    def is_alive(self) -> bool:
        return not self._closed and self._process.is_alive()

    def cleanup(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._process.is_alive():
                try:
                    self._connection.send((_CANCEL,))
                except (BrokenPipeError, EOFError, OSError):
                    pass
                self._process.join(self._cooperative_timeout_s)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(self._terminate_timeout_s)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(self._kill_timeout_s)
            if self._process.is_alive():
                raise RuntimeError("isolated capture child resisted bounded cleanup")
            self._connection.close()
            self._process.close()
            self._closed = True


class SpawnIsolatedAttemptRunner(CaptureAttemptRunner):
    """Run one station in a fresh ``spawn`` interpreter with bounded cleanup."""

    def __init__(
        self,
        station: V5CaptureStation,
        catalog_credential: str,
        batch_id: CaptureBatchId,
        work_factory: IsolatedAttemptWorkFactory,
        *,
        supervisor: SpawnProcessSupervisor | None = None,
        process_context: SpawnContext | None = None,
        poll_interval_s: float = 0.01,
        post_release_dispatch_delay_s: float = 0.0,
        monotonic: Callable[[], float] = time.monotonic,
        delay: Callable[[float], None] = time.sleep,
        cooperative_cleanup_timeout_s: float = 0.25,
        terminate_timeout_s: float = 2.0,
        kill_timeout_s: float = 2.0,
    ) -> None:
        _positive_timeout(poll_interval_s, "poll_interval_s")
        _nonnegative_duration(
            post_release_dispatch_delay_s, "post_release_dispatch_delay_s"
        )
        _positive_timeout(
            cooperative_cleanup_timeout_s, "cooperative_cleanup_timeout_s"
        )
        _positive_timeout(terminate_timeout_s, "terminate_timeout_s")
        _positive_timeout(kill_timeout_s, "kill_timeout_s")
        context = process_context or multiprocessing.get_context("spawn")
        if context.get_start_method() != "spawn":
            raise ValueError("capture isolation requires the spawn start method")
        if not catalog_credential:
            raise ValueError("catalog credential cannot be empty")
        self._station = station
        self._catalog_credential = catalog_credential
        self._batch_id = batch_id
        self._work_factory = work_factory
        self._supervisor = supervisor or SpawnProcessSupervisor()
        self._context = context
        self._poll_interval_s = poll_interval_s
        self._post_release_dispatch_delay_s = post_release_dispatch_delay_s
        self._monotonic = monotonic
        self._delay = delay
        self._cooperative_cleanup_timeout_s = cooperative_cleanup_timeout_s
        self._terminate_timeout_s = terminate_timeout_s
        self._kill_timeout_s = kill_timeout_s
        self._last_child_pid: int | None = None

    @property
    def last_child_pid(self) -> int | None:
        """PID evidence for local supervision and component tests."""

        return self._last_child_pid

    def run(
        self,
        attempt: ExpectedCaptureAttempt,
        control: CaptureAttemptControl,
    ) -> CaptureAttemptRunResult:
        parent_connection, child_connection = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=_child_main,
            args=(
                child_connection,
                self._work_factory,
                self._station,
                self._catalog_credential,
                self._batch_id,
                attempt,
                os.getpid(),
            ),
            name=f"v5-capture-{attempt.attempt_id}",
        )
        handle: _ProcessHandle | None = None
        try:
            handle = self._supervisor.start(
                process,
                parent_connection,
                cooperative_timeout_s=self._cooperative_cleanup_timeout_s,
                terminate_timeout_s=self._terminate_timeout_s,
                kill_timeout_s=self._kill_timeout_s,
            )
            self._last_child_pid = process.pid
            child_connection.close()
            message = self._receive(parent_connection, process, control)
            if message != (_READY,):
                raise _runner_failure_from_message(
                    message, "isolated capture child failed before readiness"
                )
            if not control.ready_and_wait_for_release():
                raise RuntimeError("isolated capture cancelled before release")
            self._wait_for_post_release_dispatch(control)
            parent_connection.send((_RELEASE,))
            message = self._receive(parent_connection, process, control)
            if _is_failure_message(message):
                raise _runner_failure_from_message(
                    message, "isolated capture child failed"
                )
            if (
                not isinstance(message, tuple)
                or len(message) != 2
                or message[0] != _RESULT
                or not isinstance(message[1], CaptureAttemptRunResult)
            ):
                raise RuntimeError("isolated capture child failed")
            return message[1]
        except (EOFError, BrokenPipeError, OSError) as error:
            raise RuntimeError("isolated capture child failed") from error
        finally:
            if handle is not None:
                handle.cleanup()
                self._supervisor.discard(handle)
            else:
                parent_connection.close()
            try:
                child_connection.close()
            except OSError:
                pass

    def _receive(
        self,
        connection: Connection,
        process: BaseProcess,
        control: CaptureAttemptControl,
    ) -> object:
        while True:
            if connection.poll(self._poll_interval_s):
                return connection.recv()
            if control.cancelled:
                raise RuntimeError("isolated capture cancelled")
            if not process.is_alive():
                if connection.poll():
                    return connection.recv()
                raise RuntimeError("isolated capture child exited")

    def _wait_for_post_release_dispatch(self, control: CaptureAttemptControl) -> None:
        if self._post_release_dispatch_delay_s == 0:
            return
        deadline = self._monotonic() + self._post_release_dispatch_delay_s
        while True:
            if control.cancelled:
                raise RuntimeError("isolated capture cancelled before dispatch")
            remaining_s = deadline - self._monotonic()
            if remaining_s <= 0:
                return
            self._delay(min(remaining_s, self._poll_interval_s))


def _child_main(
    connection: Connection,
    factory: IsolatedAttemptWorkFactory,
    station: V5CaptureStation,
    catalog_credential: str,
    batch_id: CaptureBatchId,
    attempt: ExpectedCaptureAttempt,
    expected_parent_pid: int,
) -> None:
    _arm_parent_death_signal(expected_parent_pid)
    _silence_child_standard_streams()
    _scrub_child_environment()
    work: IsolatedAttemptWork | None = None
    result: CaptureAttemptRunResult | None = None
    failure_reason: CaptureAttemptFailureReason | None = None
    try:
        try:
            work = factory.build(station, catalog_credential, batch_id)
        except BaseException:  # noqa: BLE001 - never disclose child/driver details
            failure_reason = CaptureAttemptFailureReason.CHILD_BUILD
        if work is not None:
            try:
                work.preflight()
            except IsolatedAttemptPhaseFailure as error:
                failure_reason = error.reason
            except BaseException:  # noqa: BLE001 - fixed fallback only
                failure_reason = CaptureAttemptFailureReason.CYCLE_PREFLIGHT
        if failure_reason is None:
            connection.send((_READY,))
            if connection.recv() != (_RELEASE,):
                return
            try:
                result = work.capture(attempt) if work is not None else None
            except IsolatedAttemptPhaseFailure as error:
                failure_reason = error.reason
            except BaseException:  # noqa: BLE001 - fixed fallback only
                failure_reason = CaptureAttemptFailureReason.CAPTURE_ENGINE
    finally:
        if work is not None:
            try:
                work.close(10.0)
            except BaseException:  # noqa: BLE001 - cleanup failure is terminal
                failure_reason = CaptureAttemptFailureReason.CHILD_CLEANUP
        if failure_reason is not None or result is None:
            reason = failure_reason or CaptureAttemptFailureReason.CAPTURE_ENGINE
            _safe_send(connection, (_FAILED, reason.value))
        else:
            _safe_send(connection, (_RESULT, result))
        connection.close()


def _safe_send(connection: Connection, value: object) -> None:
    try:
        connection.send(value)
    except (BrokenPipeError, EOFError, OSError):
        pass


def _is_failure_message(message: object) -> bool:
    return isinstance(message, tuple) and len(message) == 2 and message[0] == _FAILED


def _runner_failure_from_message(
    message: object, fallback_message: str
) -> CaptureAttemptRunnerFailure | RuntimeError:
    if _is_failure_message(message):
        assert isinstance(message, tuple)  # narrowed by the fixed-shape check
        try:
            reason = CaptureAttemptFailureReason(message[1])
        except (TypeError, ValueError):
            pass
        else:
            return CaptureAttemptRunnerFailure(reason)
    return RuntimeError(fallback_message)


def _arm_parent_death_signal(expected_parent_pid: int) -> None:
    """Kill the radio-owning child if its mode-lock-owning parent disappears."""

    if not sys.platform.startswith("linux"):
        raise RuntimeError("capture isolation requires Linux parent-death signaling")
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = (
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    )
    prctl.restype = ctypes.c_int
    if prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "cannot arm parent-death signal")
    if os.getppid() != expected_parent_pid:
        os.kill(os.getpid(), signal.SIGKILL)


def _silence_child_standard_streams() -> None:
    """Keep native driver output outside the machine-readable parent channel."""

    descriptor = os.open(os.devnull, os.O_RDWR | os.O_CLOEXEC)
    try:
        for target in (0, 1, 2):
            os.dup2(descriptor, target, inheritable=False)
    finally:
        if descriptor > 2:
            os.close(descriptor)


def _scrub_child_environment() -> None:
    """Retain only non-secret runtime loader and locale inputs."""

    allowed = {
        key: value
        for key in (
            "LANG",
            "LC_ALL",
            "LD_LIBRARY_PATH",
            "PATH",
            "PYTHONPATH",
            "TZ",
        )
        if (value := os.environ.get(key)) is not None
    }
    os.environ.clear()
    os.environ.update(allowed)


def _positive_timeout(value: float, field: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field} must be a finite positive number")


def _nonnegative_duration(value: float, field: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{field} must be a finite non-negative number")
