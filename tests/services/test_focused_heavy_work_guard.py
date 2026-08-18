from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from leo_flow.adapters.focused_continuous_sqlite import FocusedContinuousRecordV0_1
from leo_flow.adapters.optional_heavy_work_admission import (
    AtomicFocusedCaptureGuardPublisherV0_1,
    LocalCaptureAwareHeavyWorkAdmissionV0_1,
)
from leo_flow.contracts.optional_heavy_work_admission import (
    FocusedCaptureGuardV0_1,
    HeavyWorkResourceSnapshotV0_1,
)
from leo_flow.deployments.gauss_focused_continuous_operator import (
    _optional_work_guard_bounds,
    _publish_guard,
)


class _UnavailablePublisher:
    def publish(self, _snapshot: object) -> None:
        raise OSError("optional status unavailable")


class _Journal:
    def incomplete(self) -> tuple[()]:
        return ()


def test_optional_guard_publication_failure_never_blocks_capture() -> None:
    _publish_guard(  # type: ignore[arg-type]
        _UnavailablePublisher(),
        _Journal(),  # type: ignore[arg-type]
        {},
        active=True,
        guard_from_utc_ns=10,
        guard_until_utc_ns=20,
    )


def test_forty_second_buffer_allows_only_early_off_window_claims(
    tmp_path: Path,
) -> None:
    requested = 2_000_000_000_000
    args = SimpleNamespace(
        duration_seconds=60,
        optional_work_guard_buffer_seconds=40,
    )
    record = FocusedContinuousRecordV0_1(
        0,
        "focused_loop_00000000_guard",
        requested,
        "sha256:" + "a" * 64,
        tmp_path / "state",
        "cbatch_guard_u000",
        "planned",
    )
    guard_from, guard_until = _optional_work_guard_bounds(args, record)
    path = tmp_path / "guard.json"
    AtomicFocusedCaptureGuardPublisherV0_1(path).publish(
        FocusedCaptureGuardV0_1(
            guard_from - 1_000_000_000,
            guard_until + 1_000_000_000,
            guard_from,
            guard_until,
            0,
            6,
            True,
        )
    )

    def decision(at_utc_ns: int):  # type: ignore[no-untyped-def]
        gate = LocalCaptureAwareHeavyWorkAdmissionV0_1(
            path,
            clock_ns=lambda: at_utc_ns,
            maximum_focused_backlog=64,
            host_cpu_cores=24,
            reserved_cpu_cores=8,
            estimated_claim_cpu_cores=1,
            minimum_memory_available_bytes=8 * 1024**3,
            maximum_io_pressure_avg10=80,
            maximum_optional_concurrency=1,
            resource_probe=lambda: HeavyWorkResourceSnapshotV0_1(
                at_utc_ns, 24, 4, 32 * 1024**3, 70
            ),
        )
        result, permit = gate.acquire()
        if permit is not None:
            permit.release()
        return result

    assert decision(guard_from - 1).admitted
    assert decision(guard_from).reason == "capture-guard-active"
    assert decision(requested).reason == "capture-guard-active"
    assert decision(guard_until).reason == "capture-guard-active"
