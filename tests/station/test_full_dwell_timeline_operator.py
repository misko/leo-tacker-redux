from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

from leo_station.full_dwell_timeline_operator import _work_request, main
from tests.recording_analysis.test_starlink_full_dwell_timeline_product import _case


class _Cycle:
    def __init__(self, outcomes: list[tuple[int, bool]]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def run_cycle(self) -> tuple[int, bool]:
        result = self.outcomes[self.calls]
        self.calls += 1
        return result


def test_work_identity_is_exact_replay_deterministic() -> None:
    _view, request, _bundle = _case(19)
    first = _work_request(
        request.recording_object_ref,
        1_800_000_000_000_000_000,
        request.plan,
        request.stream_selections,
    )
    assert first == _work_request(
        request.recording_object_ref,
        1_800_000_000_000_000_000,
        request.plan,
        request.stream_selections,
    )
    changed = _work_request(
        request.recording_object_ref,
        1_800_000_000_000_000_000,
        replace(request.plan, maximum_refinements_per_stream=3),
        request.stream_selections,
    )
    assert first["work_id"] != changed["work_id"]


def test_once_is_one_bounded_admission_and_processing_cycle(tmp_path) -> None:
    cycle = _Cycle([(2, True)])
    output = io.StringIO()

    def build(*_args, **_kwargs):
        return cycle

    assert (
        main(
            [
                "--credential-directory",
                str(tmp_path),
                "--maximum-admissions",
                "2",
                "--once",
            ],
            stdout=output,
            cycle_builder=build,
        )
        == 0
    )
    assert cycle.calls == 1
    assert output.getvalue() == (
        '{"admitted": 2, "event": '
        '"prompt_full_dwell_timeline_cycle_complete", "processed": true}\n'
    )


def test_operator_failure_is_sanitized_and_terminal(tmp_path) -> None:
    errors = io.StringIO()

    def fail(*_args, **_kwargs):
        raise RuntimeError("sensitive catalog detail")

    assert (
        main(
            ["--credential-directory", str(tmp_path), "--once"],
            stderr=errors,
            cycle_builder=fail,
        )
        == 4
    )
    assert errors.getvalue() == '{"event":"prompt_full_dwell_timeline_cycle_failed"}\n'


def test_unit_has_resource_and_cancellation_bounds() -> None:
    unit = Path(
        "deploy/gauss-prompt-full-dwell-v1/leo-gauss-prompt-full-dwell.service.in",
    ).read_text(encoding="utf-8")
    for required in (
        "CPUQuota=300%",
        "MemoryMax=3G",
        "TasksMax=8",
        "TimeoutStopSec=30s",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
    ):
        assert required in unit
    assert "DeviceAllow" not in unit
    assert "pipeline-mode.lock" not in unit
