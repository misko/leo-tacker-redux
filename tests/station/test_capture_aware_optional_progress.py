from __future__ import annotations

from io import StringIO
from pathlib import Path

from leo_flow.adapters.optional_heavy_work_admission import (
    AtomicFocusedCaptureGuardPublisherV0_1,
    LocalCaptureAwareHeavyWorkAdmissionV0_1,
)
from leo_flow.contracts.optional_heavy_work_admission import (
    FocusedCaptureGuardV0_1,
    HeavyWorkResourceSnapshotV0_1,
)
from leo_station.adaptive_response_operator import main

NOW = 2_000_000_000_000


class _Cycle:
    calls = 0

    def run_once(self) -> bool:
        type(self).calls += 1
        return True


def test_optional_work_progresses_between_multiple_capture_guards(
    tmp_path: Path,
) -> None:
    path = tmp_path / "guard.json"
    publisher = AtomicFocusedCaptureGuardPublisherV0_1(path)

    def admission_builder(
        _path: Path, **_kwargs: object
    ) -> LocalCaptureAwareHeavyWorkAdmissionV0_1:
        return LocalCaptureAwareHeavyWorkAdmissionV0_1(
            path,
            clock_ns=lambda: NOW,
            maximum_focused_backlog=2,
            host_cpu_cores=24,
            reserved_cpu_cores=8,
            estimated_claim_cpu_cores=1,
            minimum_memory_available_bytes=8 * 1024**3,
            maximum_io_pressure_avg10=5,
            maximum_optional_concurrency=1,
            resource_probe=lambda: HeavyWorkResourceSnapshotV0_1(
                NOW, 24, 4, 32 * 1024**3, 0
            ),
        )

    argv = [
        "--credential-directory",
        str(tmp_path),
        "--capture-guard-status",
        str(path),
        "--once",
    ]
    _Cycle.calls = 0
    for active, guard_from in ((True, NOW - 1), (True, NOW + 1)) * 2:
        publisher.publish(
            FocusedCaptureGuardV0_1(
                NOW - 10, NOW + 100, guard_from, NOW + 10, 0, 6, active
            )
        )
        assert (
            main(
                argv,
                stdout=StringIO(),
                service_builder=lambda *_args, **_kwargs: _Cycle(),
                admission_builder=admission_builder,
            )
            == 0
        )
    assert _Cycle.calls == 2
