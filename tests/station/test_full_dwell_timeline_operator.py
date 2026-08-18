from __future__ import annotations

import io
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from leo_station.full_dwell_timeline_operator import (
    BoundedTimelineAdmissionV0_1,
    _work_request,
    main,
)
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


def test_targeted_admitter_never_uses_generic_candidate_backfill() -> None:
    view, request, _bundle = _case(19)
    segment = view.manifest.segments[0]
    manifest = replace(
        view.manifest,
        segments=(
            replace(
                segment,
                requested=replace(
                    segment.requested, tags=(("channel", 4), ("edge", "lower"))
                ),
            ),
        ),
    )

    class ManifestView:
        def __init__(self) -> None:
            self.manifest = manifest

    class Work:
        candidate_calls = 0

        def newest_candidate_ids(self, _maximum: int):  # type: ignore[no-untyped-def]
            self.candidate_calls += 1
            raise AssertionError("targeted admission must not list generic candidates")

        def receiver_lnbs(self, _recording_id, _at):  # type: ignore[no-untyped-def]
            return {
                selection.receiver_chain_id: (selection.radio_id, selection.lnb_id)
                for selection in request.stream_selections
            }

        def admit(self, recording_ref, body):  # type: ignore[no-untyped-def]
            assert recording_ref == request.recording_object_ref
            assert body["recording_id"] == str(request.recording_id)
            return True

    class Recordings:
        def get(self, recording_id):  # type: ignore[no-untyped-def]
            assert recording_id == request.recording_id
            return type("Published", (), {"recording_object": request.recording_object_ref})

    class Reader:
        @contextmanager
        def open(self, recording_ref):  # type: ignore[no-untyped-def]
            assert recording_ref == request.recording_object_ref
            yield ManifestView()

    work = Work()
    admission = BoundedTimelineAdmissionV0_1(
        work,  # type: ignore[arg-type]
        Recordings(),  # type: ignore[arg-type]
        Reader(),  # type: ignore[arg-type]
        maximum_admissions=1,
        tile_sample_count=8,
        maximum_refinements_per_stream=4,
        recording_ids=(request.recording_id,),
    )
    assert admission.admit() == 1
    assert work.candidate_calls == 0


def test_once_is_one_bounded_admission_and_processing_cycle(tmp_path) -> None:
    cycle = _Cycle([(2, True)])
    output = io.StringIO()
    seen: list[dict[str, object]] = []

    def build(*_args, **kwargs):
        seen.append(kwargs)
        return cycle

    assert (
        main(
            [
                "--credential-directory",
                str(tmp_path),
                "--maximum-admissions",
                "2",
                "--recording-id",
                "rec_priority_a",
                "--recording-id",
                "rec_priority_b",
                "--once",
            ],
            stdout=output,
            cycle_builder=build,
        )
        == 0
    )
    assert cycle.calls == 1
    assert tuple(map(str, seen[0]["recording_ids"])) == (
        "rec_priority_a",
        "rec_priority_b",
    )
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
        "--capture-guard-status %t/leo-flow-optional-heavy/guard.json",
        "--maximum-focused-backlog 64",
        "--host-cpu-cores 24",
        "--reserved-cpu-cores 8",
        "--estimated-claim-cpu-cores 3",
        "--maximum-optional-concurrency 1",
    ):
        assert required in unit
    assert "DeviceAllow" not in unit
    assert "pipeline-mode.lock" not in unit


def test_deployment_inventory_keeps_one_guarded_worker_and_tail_coverage() -> None:
    inventory = Path(
        "deploy/gauss-prompt-full-dwell-v1/deployment.json"
    ).read_text(encoding="utf-8")
    assert '"focused_admit_only": true' in inventory
    assert '"maximum_optional_concurrency": 1' in inventory
    assert '"maximum_focused_backlog": 64' in inventory
    assert '"claims_during_capture_guard": false' in inventory
    assert '"retains_short_tail": true' in inventory
    assert '"maximum_elapsed_seconds": 120' in inventory
