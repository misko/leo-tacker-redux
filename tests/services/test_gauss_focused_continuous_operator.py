from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from leo_flow.adapters.focused_continuous_sqlite import (
    FocusedContinuousRecordV0_1,
    SQLiteFocusedContinuousJournalV0_1,
)
from leo_flow.contracts.core import Digest
from leo_flow.deployments.gauss_focused_continuous_operator import (
    _analysis_command,
    _capture_command,
    _parser,
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
