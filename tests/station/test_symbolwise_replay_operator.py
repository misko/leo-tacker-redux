from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from leo_flow.contracts.optional_heavy_work_admission import (
    HeavyWorkAdmissionDecisionV0_1,
)
from leo_station import symbolwise_replay_operator


class _Service:
    def __init__(self, progressed: bool = True) -> None:
        self.progressed = progressed
        self.calls = 0

    def run_once(self) -> bool:
        self.calls += 1
        return self.progressed


class _Permit:
    def __init__(self) -> None:
        self.releases = 0

    def release(self) -> None:
        self.releases += 1


class _Admission:
    def __init__(self, admitted: bool, permit=None) -> None:
        self.admitted = admitted
        self.permit = permit
        self.calls = 0

    def acquire(self):
        self.calls += 1
        return (
            HeavyWorkAdmissionDecisionV0_1(
                self.admitted, "capture-safe" if self.admitted else "capture-guard"
            ),
            self.permit,
        )


def _argv() -> list[str]:
    return [
        "--credential-directory",
        "/credentials",
        "--capture-guard-status",
        "/run/leo-flow/capture-guard.json",
        "--once",
    ]


def test_capture_guard_denial_does_not_claim_expensive_work() -> None:
    service = _Service()
    admission = _Admission(False)
    stdout = StringIO()

    result = symbolwise_replay_operator.main(
        _argv(),
        stdout=stdout,
        service_builder=lambda *_args, **_kwargs: service,
        admission_builder=lambda *_args, **_kwargs: admission,
    )

    assert result == 0
    assert service.calls == 0
    assert admission.calls == 1
    assert json.loads(stdout.getvalue()) == {
        "event": "symbolwise_replay_cycle_paused",
        "reason": "capture-guard",
    }


def test_one_admitted_cycle_releases_shared_optional_work_permit() -> None:
    service = _Service()
    permit = _Permit()
    admission = _Admission(True, permit)
    observed = {}
    stdout = StringIO()

    def build(path: Path, **kwargs):
        observed.update(path=path, **kwargs)
        return service

    result = symbolwise_replay_operator.main(
        _argv(),
        stdout=stdout,
        service_builder=build,
        admission_builder=lambda path, **kwargs: (
            observed.update(guard_path=path, admission_kwargs=kwargs) or admission
        ),
    )

    assert result == 0
    assert service.calls == 1
    assert permit.releases == 1
    assert observed["path"] == Path("/credentials")
    assert observed["guard_path"] == Path("/run/leo-flow/capture-guard.json")
    assert json.loads(stdout.getvalue()) == {
        "event": "symbolwise_replay_cycle_complete",
        "processed": True,
    }


def test_process_boundary_never_prints_dependency_secrets() -> None:
    stderr = StringIO()

    def fail(*_args, **_kwargs):
        raise RuntimeError("postgresql://secret")

    result = symbolwise_replay_operator.main(
        _argv(), stdout=StringIO(), stderr=stderr, service_builder=fail
    )

    assert result == 4
    assert stderr.getvalue() == '{"event":"symbolwise_replay_cycle_failed"}\n'
    assert "secret" not in stderr.getvalue()
