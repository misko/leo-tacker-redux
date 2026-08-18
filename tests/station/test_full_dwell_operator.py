from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from leo_flow.services.starlink_full_dwell_producer import (
    FullDwellAdmissionResultV0_1,
)
from leo_station import full_dwell_operator


class Service:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)

    def run_once(self):
        return next(self.outcomes)


def test_once_emits_bounded_cycle_without_mode_lock_or_capture_dependency() -> None:
    stdout, stderr = StringIO(), StringIO()
    values = {}

    def build(path: Path, **kwargs):
        values.update(path=path, **kwargs)
        return Service(((FullDwellAdmissionResultV0_1(2, 3, False), True),))

    result = full_dwell_operator.main(
        ["--credential-directory", "/credentials", "--once"],
        stdout=stdout,
        stderr=stderr,
        service_builder=build,
    )
    assert result == 0 and not stderr.getvalue()
    assert values == {
        "path": Path("/credentials"),
        "worker_id": "gauss-full-dwell-1",
        "maximum_active": 8,
        "maximum_admissions_per_cycle": 2,
    }
    assert json.loads(stdout.getvalue()) == {
        "event": "full_dwell_cycle_complete",
        "admitted": 2,
        "active_backlog": 3,
        "saturated": False,
        "processed": True,
    }


def test_loop_polls_only_when_idle() -> None:
    sleeps = []
    service = Service(
        (
            (FullDwellAdmissionResultV0_1(1, 1, False), True),
            (FullDwellAdmissionResultV0_1(0, 0, False), False),
        )
    )

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise KeyboardInterrupt

    try:
        full_dwell_operator.main(
            ["--credential-directory", "/credentials", "--poll-seconds", "7"],
            stdout=StringIO(),
            stderr=StringIO(),
            service_builder=lambda *args, **kwargs: service,
            sleeper=sleep,
        )
    except KeyboardInterrupt:
        pass
    assert sleeps == [7.0]


def test_failure_is_sanitized() -> None:
    stderr = StringIO()

    def fail(*args, **kwargs):
        raise RuntimeError("dsn secret")

    assert full_dwell_operator.main(
        ["--credential-directory", "/credentials", "--once"],
        stdout=StringIO(),
        stderr=stderr,
        service_builder=fail,
    ) == 4
    assert stderr.getvalue() == '{"event":"full_dwell_cycle_failed"}\n'
    assert "secret" not in stderr.getvalue()
