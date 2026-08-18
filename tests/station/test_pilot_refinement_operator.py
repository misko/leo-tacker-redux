from __future__ import annotations

import io
from pathlib import Path

from leo_station.pilot_refinement_operator import main


class _Cycle:
    def __init__(self, values: list[bool]) -> None:
        self._values = values

    def run_once(self) -> bool:
        return self._values.pop(0)


def test_pilot_refinement_operator_runs_one_bounded_cycle() -> None:
    stdout = io.StringIO()
    captured = {}

    def build(_credentials, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return _Cycle([True])

    assert (
        main(
            [
                "--credential-directory",
                "/credentials",
                "--worker-id",
                "worker-a",
                "--lease-ttl-seconds",
                "3600",
                "--once",
            ],
            stdout=stdout,
            service_builder=build,
        )
        == 0
    )
    assert captured == {"worker_id": "worker-a", "lease_ttl_s": 3600.0}
    assert stdout.getvalue() == (
        '{"event": "pilot_refinement_cycle_complete", "processed": true}\n'
    )


def test_pilot_refinement_operator_sanitizes_failures() -> None:
    stderr = io.StringIO()

    def fail(_credentials, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("private failure")

    assert (
        main(
            ["--credential-directory", "/credentials", "--once"],
            stderr=stderr,
            service_builder=fail,
        )
        == 4
    )
    assert stderr.getvalue() == '{"event":"pilot_refinement_cycle_failed"}\n'


def test_pilot_refinement_unit_is_independent_and_resource_bounded() -> None:
    unit = Path(
        "deploy/gauss-pilot-refinement-v1/leo-gauss-pilot-refinement.service.in",
    ).read_text(encoding="utf-8")

    assert "leo-gauss-pilot-refinement" in unit
    assert "CPUQuota=200%" in unit
    assert "MemoryMax=4G" in unit
    assert "Nice=15" in unit
    assert "leo-gauss-focused" not in unit
