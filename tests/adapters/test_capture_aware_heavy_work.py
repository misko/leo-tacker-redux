from __future__ import annotations

from pathlib import Path

from leo_flow.adapters.optional_heavy_work_admission import (
    AtomicFocusedCaptureGuardPublisherV0_1,
    LocalCaptureAwareHeavyWorkAdmissionV0_1,
    OwnershipFencedAtomicFocusedCaptureGuardPublisherV0_1,
)
from leo_flow.contracts.optional_heavy_work_admission import (
    FocusedCaptureGuardV0_1,
    HeavyWorkAdmissionDecisionV0_1,
    HeavyWorkResourceSnapshotV0_1,
    decode_focused_capture_guard_v0_1,
)

NOW = 1_000_000_000_000


def _resources(
    *, load: float = 4.0, memory: int = 32 * 1024**3, io: float = 1.0
) -> HeavyWorkResourceSnapshotV0_1:
    return HeavyWorkResourceSnapshotV0_1(NOW, 24, load, memory, io)


def _gate(
    path: Path,
    *,
    resources: HeavyWorkResourceSnapshotV0_1 | None = None,
    backlog: int = 0,
    active: bool = False,
    guard_from: int = NOW + 10,
    guard_until: int = NOW + 20,
) -> LocalCaptureAwareHeavyWorkAdmissionV0_1:
    AtomicFocusedCaptureGuardPublisherV0_1(path).publish(
        FocusedCaptureGuardV0_1(
            NOW - 10,
            NOW + 100,
            guard_from,
            guard_until,
            backlog,
            6,
            active,
        )
    )
    return LocalCaptureAwareHeavyWorkAdmissionV0_1(
        path,
        clock_ns=lambda: NOW,
        maximum_focused_backlog=2,
        host_cpu_cores=24,
        reserved_cpu_cores=8,
        estimated_claim_cpu_cores=2,
        minimum_memory_available_bytes=8 * 1024**3,
        maximum_io_pressure_avg10=5.0,
        maximum_optional_concurrency=1,
        resource_probe=lambda: resources or _resources(),
    )


def test_guard_transitions_pause_only_during_sampling_interval(tmp_path: Path) -> None:
    path = tmp_path / "guard.json"
    decision, permit = _gate(path, active=True, guard_from=NOW - 1).acquire()
    assert (decision.admitted, decision.reason, permit) == (
        False,
        "capture-guard-active",
        None,
    )
    decision, permit = _gate(path, active=True, guard_from=NOW + 1).acquire()
    assert decision.admitted is True
    assert permit is not None
    permit.release()


def test_successor_takeover_fences_stale_predecessor_finalization(
    tmp_path: Path,
) -> None:
    path = tmp_path / "guard.json"
    predecessor = OwnershipFencedAtomicFocusedCaptureGuardPublisherV0_1(
        path, token_factory=lambda: "predecessor"
    )
    predecessor_guard = FocusedCaptureGuardV0_1(
        NOW - 1, NOW + 99, NOW + 9, NOW + 19, 3, 5, True
    )
    assert predecessor.publish(predecessor_guard) is True
    successor = OwnershipFencedAtomicFocusedCaptureGuardPublisherV0_1(
        path, token_factory=lambda: "successor"
    )
    successor_guard = FocusedCaptureGuardV0_1(
        NOW, NOW + 100, NOW + 10, NOW + 20, 2, 4, True
    )
    stale_active_guard = FocusedCaptureGuardV0_1(
        NOW + 1, NOW + 101, NOW + 11, NOW + 21, 1, 3, True
    )
    stale_final_guard = FocusedCaptureGuardV0_1(
        NOW + 2, NOW + 102, NOW + 2, NOW + 2, 0, 0, False
    )

    assert successor.publish(successor_guard) is True
    assert predecessor.publish(stale_active_guard) is False
    assert predecessor.publish(stale_final_guard) is False
    assert decode_focused_capture_guard_v0_1(path.read_bytes()) == successor_guard


def test_missing_and_stale_guard_fail_closed_without_capture_dependency(
    tmp_path: Path,
) -> None:
    path = tmp_path / "guard.json"
    gate = _gate(path)
    path.unlink()
    assert gate.acquire()[0].reason == "guard-unavailable"
    AtomicFocusedCaptureGuardPublisherV0_1(path).publish(
        FocusedCaptureGuardV0_1(NOW - 20, NOW - 1, NOW - 20, NOW - 10, 0, 0, False)
    )
    assert gate.acquire()[0].reason == "guard-stale"


def test_backlog_cpu_memory_and_io_decisions_are_independent(tmp_path: Path) -> None:
    path = tmp_path / "guard.json"
    assert _gate(path, backlog=3).acquire()[0].reason == "focused-backlog-high"
    assert (
        _gate(path, resources=_resources(load=15)).acquire()[0].reason
        == "cpu-pressure-high"
    )
    assert (
        _gate(path, resources=_resources(memory=1024)).acquire()[0].reason
        == "memory-pressure-high"
    )
    assert (
        _gate(path, resources=_resources(io=6)).acquire()[0].reason
        == "io-pressure-high"
    )
    wrong_topology = HeavyWorkResourceSnapshotV0_1(NOW, 23, 4, 32 * 1024**3, 1)
    assert (
        _gate(path, resources=wrong_topology).acquire()[0].reason
        == "resource-topology-mismatch"
    )


def test_slot_is_held_for_the_whole_fenced_lease(tmp_path: Path) -> None:
    path = tmp_path / "guard.json"
    first = _gate(path)
    second = _gate(path)
    decision, permit = first.acquire()
    assert decision.admitted and permit is not None
    assert second.acquire()[0].reason == "optional-concurrency-full"
    permit.release()
    next_decision, next_permit = second.acquire()
    assert next_decision.admitted and next_permit is not None
    next_permit.release()


def test_resource_snapshot_observed_during_probe_is_not_future_dated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "guard.json"
    AtomicFocusedCaptureGuardPublisherV0_1(path).publish(
        FocusedCaptureGuardV0_1(
            NOW - 10,
            NOW + 100,
            NOW + 10,
            NOW + 20,
            0,
            6,
            False,
        )
    )
    clock_values = iter((NOW, NOW + 2))
    gate = LocalCaptureAwareHeavyWorkAdmissionV0_1(
        path,
        clock_ns=lambda: next(clock_values),
        maximum_focused_backlog=2,
        host_cpu_cores=24,
        reserved_cpu_cores=8,
        estimated_claim_cpu_cores=2,
        minimum_memory_available_bytes=8 * 1024**3,
        maximum_io_pressure_avg10=5.0,
        maximum_optional_concurrency=1,
        resource_probe=lambda: HeavyWorkResourceSnapshotV0_1(
            NOW + 1, 24, 4.0, 32 * 1024**3, 1.0
        ),
    )

    decision, permit = gate.acquire()

    assert decision == HeavyWorkAdmissionDecisionV0_1(True, "admitted")
    assert permit is not None
    permit.release()
