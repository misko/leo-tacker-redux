"""Bounded spawned workers for one campaign-scoped analysis lane."""

from __future__ import annotations

import ctypes
import multiprocessing
import os
import signal
import sys
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection, wait
from typing import Any, Protocol, cast

from leo_flow.contracts.core import UtcNs
from leo_flow.contracts.deferred_analysis import (
    DeferredAnalysisLaneResultV1,
    DeferredAnalysisLaneState,
    DeferredAnalysisStage,
    DeferredAnalysisWindowV1,
)


class DeferredAnalysisChildWorkerV1(Protocol):
    def process_one(
        self,
        stage: DeferredAnalysisStage,
        window: DeferredAnalysisWindowV1,
        worker_instance_id: str,
    ) -> bool: ...


class DeferredAnalysisLaneStateReaderV1(Protocol):
    def states(
        self, window: DeferredAnalysisWindowV1, stage: DeferredAnalysisStage
    ) -> dict[str, str]: ...


@dataclass
class _Child:
    receiver: Connection
    sender: Connection
    process: Any
    started: bool = False


class BoundedSpawnDeferredAnalysisLaneV1:
    """Run independent scoped claimers and verify durable terminal state."""

    def __init__(
        self,
        worker: DeferredAnalysisChildWorkerV1,
        state_reader: DeferredAnalysisLaneStateReaderV1,
        *,
        maximum_jobs_per_child: int = 72,
    ) -> None:
        if not 1 <= maximum_jobs_per_child <= 72:
            raise ValueError("child job bound must be within 1..72")
        self._worker = worker
        self._state_reader = state_reader
        self._maximum_jobs = maximum_jobs_per_child

    def drain(
        self,
        window: DeferredAnalysisWindowV1,
        stage: DeferredAnalysisStage,
        *,
        workers: int,
        deadline_utc_ns: UtcNs,
    ) -> DeferredAnalysisLaneResultV1:
        bound = 8 if stage.value.endswith("compute") else 4
        if not 1 <= workers <= bound:
            raise ValueError("lane worker count exceeds its reviewed bound")
        if int(deadline_utc_ns) <= time.time_ns():
            raise RuntimeError("deferred analysis lane deadline has elapsed")
        retry_delay_s = 1.0
        while True:
            children = self._spawn(window, stage, workers, deadline_utc_ns)
            self._wait(children, deadline_utc_ns)
            result = self.inspect(window, stage)
            if result.state is not DeferredAnalysisLaneState.PENDING:
                return result

            # A previous worker may still own an unexpired durable lease after
            # its supervisor exits. Exact-scope claims correctly return no
            # work in that case. Wait within the caller's deadline and retry;
            # the repository will reclaim only after the lease expires.
            remaining_s = (int(deadline_utc_ns) - time.time_ns()) / 1_000_000_000
            if remaining_s <= 0:
                raise RuntimeError("deferred analysis lane deadline elapsed")
            time.sleep(min(retry_delay_s, remaining_s))
            retry_delay_s = min(retry_delay_s * 2.0, 15.0)

    def inspect(
        self, window: DeferredAnalysisWindowV1, stage: DeferredAnalysisStage
    ) -> DeferredAnalysisLaneResultV1:
        states = self._state_reader.states(window, stage)
        expected_ids = _stage_ids(window, stage)
        if not set(states).issubset(expected_ids):
            raise RuntimeError("deferred lane state escaped its exact scope")
        parked = tuple(
            sorted(key for key, value in states.items() if value == "parked")
        )
        succeeded = sum(value == "succeeded" for value in states.values())
        retryable = len(expected_ids) - succeeded - len(parked)
        if parked:
            state = DeferredAnalysisLaneState.PARKED
        elif retryable:
            state = DeferredAnalysisLaneState.PENDING
        else:
            state = DeferredAnalysisLaneState.COMPLETE
        return DeferredAnalysisLaneResultV1(
            stage, state, len(expected_ids), succeeded, retryable, parked
        )

    def _spawn(
        self,
        window: DeferredAnalysisWindowV1,
        stage: DeferredAnalysisStage,
        workers: int,
        deadline: UtcNs,
    ) -> list[_Child]:
        context = multiprocessing.get_context("spawn")
        children: list[_Child] = []
        try:
            for index in range(workers):
                receiver, sender = context.Pipe(duplex=False)
                child = _Child(
                    receiver,
                    sender,
                    context.Process(
                        target=_lane_child_main,
                        args=(
                            sender,
                            self._worker,
                            window,
                            stage,
                            (
                                f"campaign-{window.first_success_index:03d}-"
                                f"{stage.value}-{index + 1}-of-{workers}"
                            ),
                            self._maximum_jobs,
                            deadline,
                            os.getpid(),
                        ),
                        name=f"campaign-{stage.value}-{index + 1}",
                    ),
                )
                children.append(child)
                child.process.start()
                child.started = True
                sender.close()
            return children
        except Exception:
            _reap(children)
            raise

    @staticmethod
    def _wait(children: list[_Child], deadline: UtcNs) -> None:
        pending = {child.receiver: child for child in children}
        try:
            while pending:
                remaining = (int(deadline) - time.time_ns()) / 1_000_000_000
                if remaining <= 0:
                    raise RuntimeError("deferred analysis child deadline elapsed")
                ready = wait(tuple(pending), timeout=remaining)
                if not ready:
                    raise RuntimeError("deferred analysis child deadline elapsed")
                for ready_receiver in ready:
                    receiver = cast(Connection, ready_receiver)
                    child = pending.pop(receiver)
                    try:
                        outcome = receiver.recv()
                    except (EOFError, OSError) as error:
                        raise RuntimeError("deferred analysis child failed") from error
                    child.process.join(0.1)
                    if (
                        child.process.is_alive()
                        or child.process.exitcode != 0
                        or not isinstance(outcome, tuple)
                        or len(outcome) != 2
                        or outcome[0] != "ok"
                        or not isinstance(outcome[1], int)
                    ):
                        raise RuntimeError("deferred analysis child failed")
        finally:
            _reap(children)


def _lane_child_main(
    connection: Connection,
    worker: DeferredAnalysisChildWorkerV1,
    window: DeferredAnalysisWindowV1,
    stage: DeferredAnalysisStage,
    worker_id: str,
    maximum_jobs: int,
    deadline: UtcNs,
    expected_parent_pid: int,
) -> None:
    try:
        _arm_parent_death(expected_parent_pid)
        if os.getppid() != expected_parent_pid:
            raise RuntimeError("deferred analysis parent identity changed")
        _silence_child_standard_streams()
        _scrub_child_environment()
        processed = 0
        while processed < maximum_jobs and time.time_ns() < int(deadline):
            if not worker.process_one(stage, window, worker_id):
                break
            processed += 1
        connection.send(("ok", processed))
    except Exception:  # noqa: BLE001 - sanitized process boundary
        try:
            connection.send(("error", 0))
        except (BrokenPipeError, OSError):
            pass
        raise SystemExit(1) from None
    finally:
        connection.close()


def _arm_parent_death(expected_parent_pid: int) -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("deferred analysis isolation requires Linux")
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
    if prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "parent-death signal could not be armed")
    if os.getppid() != expected_parent_pid:
        raise RuntimeError("deferred analysis parent exited before child arm")


def _silence_child_standard_streams() -> None:
    descriptor = os.open(os.devnull, os.O_RDWR | os.O_CLOEXEC)
    try:
        for target in (0, 1, 2):
            os.dup2(descriptor, target, inheritable=False)
    finally:
        if descriptor > 2:
            os.close(descriptor)


def _scrub_child_environment() -> None:
    allowed = {
        key: value
        for key in (
            "LANG",
            "LC_ALL",
            "LD_LIBRARY_PATH",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "PATH",
            "PYTHONPATH",
            "TZ",
        )
        if (value := os.environ.get(key)) is not None
    }
    os.environ.clear()
    os.environ.update(allowed)


def _stage_ids(
    window: DeferredAnalysisWindowV1, stage: DeferredAnalysisStage
) -> set[str]:
    if stage in {
        DeferredAnalysisStage.FEATURE_COMPUTE,
        DeferredAnalysisStage.FEATURE_PROJECTION,
    }:
        return {str(value) for value in window.feature_job_ids}
    if stage in {
        DeferredAnalysisStage.WATERFALL_COMPUTE,
        DeferredAnalysisStage.WATERFALL_PROJECTION,
    }:
        return {str(value) for value in window.waterfall_job_ids}
    return {str(value) for value in window.starlink_suite_job_ids}


def _reap(children: list[_Child]) -> None:
    for child in children:
        for connection in (child.receiver, child.sender):
            try:
                connection.close()
            except OSError:
                pass
    alive = [child for child in children if child.started and child.process.is_alive()]
    for child in alive:
        child.process.terminate()
    deadline = time.monotonic() + 0.25
    for child in alive:
        child.process.join(max(0.0, deadline - time.monotonic()))
    stubborn = [child for child in alive if child.process.is_alive()]
    for child in stubborn:
        child.process.kill()
    deadline = time.monotonic() + 0.25
    for child in stubborn:
        child.process.join(max(0.0, deadline - time.monotonic()))
    for child in children:
        if child.started and not child.process.is_alive():
            try:
                child.process.close()
            except (OSError, ValueError):
                pass
