from __future__ import annotations

import io
import json
import threading

import pytest

from leo_flow.services import (
    JsonLineDiagnosticSink,
    ServiceLifecycleError,
    ServiceLoop,
    ServiceState,
)


def test_startup_is_idempotent_and_one_shot_updates_health_and_json_diagnostics() -> (
    None
):
    starts: list[None] = []
    output = io.StringIO()
    service = ServiceLoop(
        service="analysis",
        instance_id="worker-1",
        start=lambda: starts.append(None),
        step=lambda: True,
        diagnostics=JsonLineDiagnosticSink(output),
        poll_interval_s=0.01,
        shutdown_timeout_s=0.1,
    )
    assert service.run_once()
    assert service.run_once()
    assert len(starts) == 1
    assert service.health().completed_units == 2
    assert service.health().ready
    service.shutdown()
    assert service.health().state is ServiceState.STOPPED
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [event["event"] for event in events] == [
        "starting",
        "ready",
        "unit_completed",
        "unit_completed",
        "draining",
        "stopped",
    ]
    assert all(event["instance_id"] == "worker-1" for event in events)


def test_idle_loop_honors_explicit_iteration_bound_and_closes() -> None:
    calls: list[float] = []
    service = ServiceLoop(
        service="capture",
        instance_id="capture-1",
        step=lambda: False,
        close=calls.append,
        poll_interval_s=0.001,
        shutdown_timeout_s=0.1,
    )
    service.run_forever(max_iterations=2)
    assert calls == [0.1]
    assert service.health().state is ServiceState.STOPPED


def test_stop_request_removes_readiness_and_refuses_another_unit() -> None:
    service = ServiceLoop(
        service="capture",
        instance_id="capture-1",
        step=lambda: True,
        poll_interval_s=0.01,
        shutdown_timeout_s=0.1,
    )
    assert service.run_once()
    service.request_stop()
    assert service.health().state is ServiceState.DRAINING
    assert not service.health().ready
    assert not service.run_once()
    service.shutdown()
    assert service.health().state is ServiceState.STOPPED


def test_failed_unit_is_not_reported_ready() -> None:
    def fail() -> bool:
        raise RuntimeError("adapter unavailable")

    service = ServiceLoop(
        service="analysis",
        instance_id="worker-1",
        step=fail,
        poll_interval_s=0.01,
        shutdown_timeout_s=0.1,
    )
    with pytest.raises(RuntimeError, match="unavailable"):
        service.run_once()
    health = service.health()
    assert health.state is ServiceState.FAILED
    assert not health.ready
    assert health.failed_units == 1
    service.shutdown()
    assert service.health().state is ServiceState.FAILED


def test_shutdown_has_a_hard_deadline_even_if_adapter_close_blocks() -> None:
    blocked = threading.Event()

    def close(timeout_s: float) -> None:
        del timeout_s
        blocked.wait(5)

    service = ServiceLoop(
        service="dashboard",
        instance_id="dashboard-1",
        step=lambda: False,
        close=close,
        poll_interval_s=0.01,
        shutdown_timeout_s=0.01,
    )
    service.run_once()
    with pytest.raises(ServiceLifecycleError, match="deadline"):
        service.shutdown()
    assert service.health().state is ServiceState.FAILED


def test_repeated_shutdown_is_idempotent_after_clean_stop() -> None:
    calls: list[float] = []
    service = ServiceLoop(
        service="dashboard",
        instance_id="dashboard-1",
        step=lambda: False,
        close=calls.append,
        poll_interval_s=0.01,
        shutdown_timeout_s=0.1,
    )
    service.run_once()
    service.shutdown()
    service.shutdown()
    assert calls == [0.1]
    with pytest.raises(ServiceLifecycleError, match="stopped"):
        service.run_once()
