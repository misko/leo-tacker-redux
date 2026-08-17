from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from leo_flow.adapters.focused_continuous_sqlite import (
    FocusedContinuousRecordV0_1,
    SQLiteFocusedContinuousJournalV0_1,
)
from leo_flow.contracts.core import Digest, canonical_digest
from leo_flow.deployments.gauss_focused_continuous_operator import (
    _analysis_command,
    _AnalysisChild,
    _capture_command,
    _parser,
    _reap,
    _recover,
    main,
)


def _args(tmp_path: Path):  # type: ignore[no-untyped-def]
    return _parser().parse_args(
        [
            "--station-a",
            str(tmp_path / "a.json"),
            "--station-b",
            str(tmp_path / "b.json"),
            "--state-root",
            str(tmp_path / "state"),
            "--capture-credential-directory",
            str(tmp_path / "capture-credentials"),
            "--analysis-config",
            str(tmp_path / "analysis.json"),
            "--analysis-credential-directory",
            str(tmp_path / "analysis-credentials"),
            "--dashboard-credential-directory",
            str(tmp_path / "dashboard-credentials"),
            "--arm",
        ]
    )


def _record(tmp_path: Path) -> FocusedContinuousRecordV0_1:
    return FocusedContinuousRecordV0_1(
        0,
        "focused_loop_00000000_abc",
        123,
        "sha256:" + "a" * 64,
        tmp_path / "state" / "focused_loop_00000000_abc",
        "cbatch_focused_loop_00000000_abc_u000",
        "captured",
    )


def test_help_describes_continuous_capture_and_async_analysis() -> None:
    text = _parser().format_help()
    assert text.startswith("usage: leo-gauss-focused-continuous")
    assert "Continuously capture" in text
    assert "asynchronously" in text
    assert "--maximum-in-flight-analyses" in text


def test_capture_child_is_capture_only_and_analysis_child_is_capture_safe(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)
    record = _record(tmp_path)
    capture = _capture_command(args, record)
    analysis = _analysis_command(args, record)

    assert "--capture-only" in capture
    assert "--confirm-definition-digest" in capture
    assert record.definition_digest in capture
    assert "--capture-safe" in analysis
    assert "--capture-definition-digest" in analysis
    assert "--completion-receipt" in analysis
    assert "--analysis-attempt-lock" in analysis
    assert record.definition_digest in analysis
    assert "--compute-workers" in analysis


def test_restart_journal_exposes_captured_work_for_redispatch(tmp_path: Path) -> None:
    journal = SQLiteFocusedContinuousJournalV0_1(tmp_path / "journal.sqlite3")
    planned = _record(tmp_path)
    planned = FocusedContinuousRecordV0_1(
        planned.sequence,
        planned.monitor_id,
        planned.requested_start_utc_ns,
        planned.definition_digest,
        planned.state_root,
        planned.batch_id,
        "planned",
    )
    journal.insert_planned(planned)
    journal.transition(0, "planned", "captured")

    assert tuple(item.state for item in journal.incomplete()) == ("captured",)


def test_user_service_is_one_continuous_loop_without_timer_or_shell_engine() -> None:
    unit = Path(
        "deploy/gauss-focused-continuous-v1/leo-focused-continuous.service.in"
    ).read_text(encoding="utf-8")
    assert "leo-gauss-focused-continuous" in unit
    assert "--maximum-in-flight-analyses 8" in unit
    assert "--compute-workers 8" in unit
    assert "Restart=no" in unit
    assert "KillMode=process" in unit
    assert "--shutdown-protocol graceful-drain-v1" in unit
    assert "RuntimeMaxSec" not in unit
    assert "PrivateDevices=yes" not in unit
    assert "bash" not in unit
    assert ".timer" not in unit


def test_main_dispatches_analysis_then_captures_next_dwell_without_waiting(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    args = _args(tmp_path)
    for path in (args.station_a, args.station_b, args.analysis_config):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    events: list[str] = []

    class FakeProcess:
        pid = 1234

        def __init__(self) -> None:
            self.poll_count = 0

        def poll(self):  # type: ignore[no-untyped-def]
            self.poll_count += 1
            events.append("poll-analysis")
            return None if self.poll_count == 1 else 0

        def terminate(self) -> None:
            events.append("terminate-analysis")

        def wait(self, timeout=None):  # type: ignore[no-untyped-def]
            return 0

        def kill(self) -> None:
            raise AssertionError("healthy analysis child must not be killed")

    monkeypatch.setattr(
        "leo_flow.deployments.gauss_focused_continuous_operator.load_v5_capture_station",
        lambda _path: SimpleNamespace(specification_digest=Digest.sha256(b"station")),
    )
    monkeypatch.setattr(
        "leo_flow.deployments.gauss_focused_continuous_operator._capture_closed",
        lambda _record: True,
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: (
            events.append("capture") or subprocess.CompletedProcess(a[0], 0)
        ),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *a, **kw: events.append("dispatch-analysis") or FakeProcess(),
    )
    monkeypatch.setattr(
        "leo_flow.deployments.gauss_focused_continuous_operator.time.sleep",
        lambda _seconds: None,
    )
    monkeypatch.setattr(
        "leo_flow.deployments.gauss_focused_continuous_operator._pid_start_ticks",
        lambda _pid: 99,
    )
    monkeypatch.setattr(
        "leo_flow.deployments.gauss_focused_continuous_operator._completion_matches",
        lambda _record: True,
    )

    argv = [
        "--station-a",
        str(args.station_a),
        "--station-b",
        str(args.station_b),
        "--state-root",
        str(args.state_root),
        "--capture-credential-directory",
        str(args.capture_credential_directory),
        "--analysis-config",
        str(args.analysis_config),
        "--analysis-credential-directory",
        str(args.analysis_credential_directory),
        "--dashboard-credential-directory",
        str(args.dashboard_credential_directory),
        "--maximum-dwells",
        "2",
        "--minimum-free-bytes",
        "1",
        "--arm",
    ]
    assert main(argv) == 0
    assert events[:4] == [
        "capture",
        "dispatch-analysis",
        "poll-analysis",
        "capture",
    ]
    journal = SQLiteFocusedContinuousJournalV0_1(args.state_root / "continuous.sqlite3")
    assert journal.next_sequence() == 2
    assert journal.incomplete() == ()


def test_recovery_adopts_only_the_exact_live_analysis_process(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    args = _args(tmp_path)
    journal = SQLiteFocusedContinuousJournalV0_1(tmp_path / "journal.sqlite3")
    record = _record(tmp_path)
    journal.insert_planned(replace(record, state="planned"))
    journal.transition(0, "planned", "captured")
    captured = journal.get(0)
    assert captured is not None
    command_digest = str(canonical_digest(tuple(_analysis_command(args, captured))))
    journal.claim_analysis_process(
        0, pid=4321, process_start_ticks=777, command_digest=command_digest
    )
    monkeypatch.setattr(
        "leo_flow.deployments.gauss_focused_continuous_operator._pid_start_ticks",
        lambda pid: 777 if pid == 4321 else None,
    )
    monkeypatch.setattr(
        "leo_flow.deployments.gauss_focused_continuous_operator.subprocess.Popen",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("live exact process must not be redispatched")
        ),
    )
    children = {}
    assert _recover(args, journal, children) is True
    assert tuple(children) == (0,)
    assert journal.get(0).state == "analysis_running"  # type: ignore[union-attr]


def test_recovery_returns_proven_dead_attempt_to_captured_before_redispatch(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    args = _args(tmp_path)
    journal = SQLiteFocusedContinuousJournalV0_1(tmp_path / "journal.sqlite3")
    record = _record(tmp_path)
    journal.insert_planned(replace(record, state="planned"))
    journal.transition(0, "planned", "captured")
    captured = journal.get(0)
    assert captured is not None
    journal.claim_analysis_process(
        0,
        pid=111,
        process_start_ticks=222,
        command_digest=str(canonical_digest(tuple(_analysis_command(args, captured)))),
    )

    class NewProcess:
        pid = 333

        def poll(self):
            return None  # type: ignore[no-untyped-def]

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0  # type: ignore[no-untyped-def]

        def kill(self):
            pass

    monkeypatch.setattr(
        "leo_flow.deployments.gauss_focused_continuous_operator._pid_start_ticks",
        lambda pid: 444 if pid == 333 else None,
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_kw: NewProcess())
    children = {}
    assert _recover(args, journal, children) is True
    running = journal.get(0)
    assert running is not None
    assert (running.state, running.analysis_pid) == ("analysis_running", 333)


def test_recovery_closes_from_valid_receipt_without_dispatch(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    args = _args(tmp_path)
    journal = SQLiteFocusedContinuousJournalV0_1(tmp_path / "journal.sqlite3")
    record = _record(tmp_path)
    journal.insert_planned(replace(record, state="planned"))
    journal.transition(0, "planned", "captured")
    monkeypatch.setattr(
        "leo_flow.deployments.gauss_focused_continuous_operator._completion_state",
        lambda _record: "valid",
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )
    assert _recover(args, journal, {}) is True
    assert journal.get(0).state == "complete"  # type: ignore[union-attr]


def test_zero_exit_without_exact_completion_evidence_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    journal = SQLiteFocusedContinuousJournalV0_1(tmp_path / "journal.sqlite3")
    record = _record(tmp_path)
    journal.insert_planned(replace(record, state="planned"))
    journal.transition(0, "planned", "captured")
    journal.claim_analysis_process(
        0,
        pid=123,
        process_start_ticks=456,
        command_digest="sha256:" + "d" * 64,
    )
    running = journal.get(0)
    assert running is not None

    class Exited:
        pid = 123

        def poll(self):
            return 0  # type: ignore[no-untyped-def]

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0  # type: ignore[no-untyped-def]

        def kill(self):
            pass

    monkeypatch.setattr(
        "leo_flow.deployments.gauss_focused_continuous_operator._completion_matches",
        lambda _record: False,
    )
    children = {0: _AnalysisChild(running, Exited())}
    assert _reap(children, journal) is False
    failed = journal.get(0)
    assert failed is not None
    assert failed.state == "failed"
    assert failed.error == "analysis-completion-evidence-missing-or-invalid"
