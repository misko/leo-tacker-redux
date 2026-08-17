"""Continuously capture focused dwells and drain their analysis asynchronously."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Protocol

from leo_flow.adapters.capture_batch_sqlite import SQLiteCaptureBatchStateStore
from leo_flow.adapters.focused_continuous_sqlite import (
    FocusedContinuousRecordV0_1,
    SQLiteFocusedContinuousJournalV0_1,
)
from leo_flow.capture.v5_station import load_v5_capture_station
from leo_flow.contracts.capture_batch import CaptureAttemptState
from leo_flow.contracts.core import CaptureBatchId, UtcNs, canonical_digest
from leo_flow.contracts.focused_analysis_completion import (
    decode_focused_analysis_completion,
)
from leo_flow.deployments.gauss_focused_capture_operator import (
    MINIMUM_LEAD_NS,
    FocusedCaptureDefinition,
)

MAXIMUM_IN_FLIGHT_ANALYSES = 8
DEFAULT_POLL_INTERVAL_S = 0.25
DEFAULT_MINIMUM_FREE_BYTES = 10 * 1024**3
DEFAULT_LEAD_SECONDS = MINIMUM_LEAD_NS // 1_000_000_000 + 15


@dataclass(slots=True)
class _AnalysisChild:
    record: FocusedContinuousRecordV0_1
    process: _Process


class _Process(Protocol):
    pid: int

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def kill(self) -> None: ...


@dataclass(slots=True)
class _RecoveredProcess:
    pid: int
    start_ticks: int
    record: FocusedContinuousRecordV0_1

    def poll(self) -> int | None:
        if _completion_matches(self.record):
            return 0
        return None if _pid_start_ticks(self.pid) == self.start_ticks else 1

    def terminate(self) -> None:
        if _pid_start_ticks(self.pid) == self.start_ticks:
            os.kill(self.pid, signal.SIGTERM)

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                assert timeout is not None
                raise subprocess.TimeoutExpired(str(self.pid), timeout)
            time.sleep(DEFAULT_POLL_INTERVAL_S)
        return self.poll() or 0

    def kill(self) -> None:
        if _pid_start_ticks(self.pid) == self.start_ticks:
            os.kill(self.pid, signal.SIGKILL)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leo-gauss-focused-continuous",
        description=(
            "Continuously capture synchronized 20-second CH4-lower dwells and "
            "dispatch exact analysis asynchronously."
        ),
    )
    parser.add_argument("--station-a", type=Path, required=True)
    parser.add_argument("--station-b", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--capture-credential-directory", type=Path, required=True)
    parser.add_argument("--analysis-config", type=Path, required=True)
    parser.add_argument("--analysis-credential-directory", type=Path, required=True)
    parser.add_argument("--dashboard-credential-directory", type=Path, required=True)
    parser.add_argument("--compute-workers", type=int, default=8)
    parser.add_argument(
        "--maximum-in-flight-analyses",
        type=int,
        default=MAXIMUM_IN_FLIGHT_ANALYSES,
    )
    parser.add_argument("--lead-seconds", type=int, default=DEFAULT_LEAD_SECONDS)
    parser.add_argument("--maximum-dwells", type=int, default=0)
    parser.add_argument(
        "--minimum-free-bytes", type=int, default=DEFAULT_MINIMUM_FREE_BYTES
    )
    parser.add_argument("--arm", action="store_true")
    parser.add_argument(
        "--shutdown-protocol",
        choices=("graceful-drain-v1",),
        default="graceful-drain-v1",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not _valid_args(args):
        return 3
    args.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    journal = SQLiteFocusedContinuousJournalV0_1(args.state_root / "continuous.sqlite3")
    failure_latch = args.state_root / "failure-latch.json"
    if failure_latch.exists():
        return 70
    stopping = False

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    children: dict[int, _AnalysisChild] = {}
    try:
        if not _recover(args, journal, children):
            _write_failure_latch(failure_latch, "recovery-failed")
            return 4
        completed_capture_count = journal.next_sequence()
        while not stopping and (
            args.maximum_dwells == 0 or completed_capture_count < args.maximum_dwells
        ):
            if not _reap(children, journal):
                _write_failure_latch(failure_latch, "analysis-failed")
                return 4
            while len(children) >= args.maximum_in_flight_analyses:
                if stopping:
                    break
                time.sleep(DEFAULT_POLL_INTERVAL_S)
                if not _reap(children, journal):
                    _write_failure_latch(failure_latch, "analysis-failed")
                    return 4
            if stopping:
                break
            if shutil.disk_usage(args.state_root).free < args.minimum_free_bytes:
                _write_failure_latch(failure_latch, "capacity-gate-failed")
                return 4
            record = _plan(args, journal)
            print(
                json.dumps(
                    {
                        "event": "focused_continuous_capture_planned",
                        "sequence": record.sequence,
                        "monitor_id": record.monitor_id,
                        "requested_start_utc_ns": record.requested_start_utc_ns,
                        "in_flight_analyses": len(children),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            result = subprocess.run(
                _capture_command(args, record), check=False, text=True
            )
            if result.returncode != 0 or not _capture_closed(record):
                journal.transition(
                    record.sequence,
                    "planned",
                    "failed",
                    error=f"capture-exit-{result.returncode}",
                )
                _write_failure_latch(failure_latch, "capture-failed")
                return 4
            journal.transition(record.sequence, "planned", "captured")
            captured = journal.get(record.sequence)
            assert captured is not None
            _dispatch(args, captured, journal, children)
            completed_capture_count += 1
        while children:
            if not _reap(children, journal):
                _write_failure_latch(failure_latch, "analysis-failed")
                return 4
            if children:
                time.sleep(DEFAULT_POLL_INTERVAL_S)
        return 0
    finally:
        for child in children.values():
            if child.process.poll() is None:
                child.process.terminate()
        for child in children.values():
            try:
                child.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.process.kill()
                child.process.wait()


def _valid_args(args: argparse.Namespace) -> bool:
    paths = (
        args.station_a,
        args.station_b,
        args.state_root,
        args.capture_credential_directory,
        args.analysis_config,
        args.analysis_credential_directory,
        args.dashboard_credential_directory,
    )
    return (
        args.arm
        and all(path.is_absolute() and ".." not in path.parts for path in paths)
        and args.station_a.is_file()
        and args.station_b.is_file()
        and args.analysis_config.is_file()
        and 1 <= args.compute_workers <= 8
        and 1 <= args.maximum_in_flight_analyses <= MAXIMUM_IN_FLIGHT_ANALYSES
        and args.lead_seconds * 1_000_000_000 >= MINIMUM_LEAD_NS
        and args.maximum_dwells >= 0
        and args.minimum_free_bytes > 0
        and args.shutdown_protocol == "graceful-drain-v1"
    )


def _plan(
    args: argparse.Namespace,
    journal: SQLiteFocusedContinuousJournalV0_1,
) -> FocusedContinuousRecordV0_1:
    sequence = journal.next_sequence()
    now = time.time_ns()
    monitor_id = f"focused_loop_{sequence:08d}_{now:x}"
    requested = now + args.lead_seconds * 1_000_000_000
    first = load_v5_capture_station(args.station_a)
    second = load_v5_capture_station(args.station_b)
    definition = FocusedCaptureDefinition(
        monitor_id,
        UtcNs(requested),
        first.specification_digest,
        second.specification_digest,
    )
    root = args.state_root / monitor_id
    record = FocusedContinuousRecordV0_1(
        sequence,
        monitor_id,
        requested,
        str(definition.digest),
        root,
        f"cbatch_{monitor_id}_u000",
        "planned",
    )
    journal.insert_planned(record)
    return record


def _capture_command(
    args: argparse.Namespace, record: FocusedContinuousRecordV0_1
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "leo_flow.deployments.gauss_focused_capture_operator",
        "--monitor-id",
        record.monitor_id,
        "--requested-start-utc-ns",
        str(record.requested_start_utc_ns),
        "--station-a",
        str(args.station_a),
        "--station-b",
        str(args.station_b),
        "--state-root",
        str(record.state_root),
        "--capture-credential-directory",
        str(args.capture_credential_directory),
        "--analysis-config",
        str(args.analysis_config),
        "--analysis-credential-directory",
        str(args.analysis_credential_directory),
        "--dashboard-credential-directory",
        str(args.dashboard_credential_directory),
        "--confirm-definition-digest",
        record.definition_digest,
        "--arm",
        "--capture-only",
    ]


def _analysis_command(
    args: argparse.Namespace, record: FocusedContinuousRecordV0_1
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "leo_flow.deployments.gauss_focused_analysis_operator",
        "--batch-database",
        str(record.state_root / "capture-batches.sqlite3"),
        "--batch-id",
        record.batch_id,
        "--analysis-config",
        str(args.analysis_config),
        "--analysis-credential-directory",
        str(args.analysis_credential_directory),
        "--dashboard-credential-directory",
        str(args.dashboard_credential_directory),
        "--compute-workers",
        str(args.compute_workers),
        "--capture-definition-digest",
        record.definition_digest,
        "--completion-receipt",
        str(_completion_path(record)),
        "--analysis-attempt-lock",
        str(record.state_root / "analysis-attempt.lock"),
        "--capture-safe",
        "--arm",
    ]


def _capture_closed(record: FocusedContinuousRecordV0_1) -> bool:
    database = record.state_root / "capture-batches.sqlite3"
    if not database.is_file():
        return False
    snapshot = SQLiteCaptureBatchStateStore(database).get(
        CaptureBatchId(record.batch_id)
    )
    return bool(
        snapshot is not None
        and snapshot.terminal
        and len(snapshot.outcomes) == 2
        and all(
            item.state is CaptureAttemptState.SUCCEEDED for item in snapshot.outcomes
        )
    )


def _dispatch(
    args: argparse.Namespace,
    record: FocusedContinuousRecordV0_1,
    journal: SQLiteFocusedContinuousJournalV0_1,
    children: dict[int, _AnalysisChild],
) -> None:
    if record.state != "captured":
        raise RuntimeError("only captured analysis may be dispatched")
    command = _analysis_command(args, record)
    process = subprocess.Popen(command, text=True)
    try:
        start_ticks = _pid_start_ticks(process.pid)
        if start_ticks is None:
            raise RuntimeError("analysis child process identity is unavailable")
        journal.claim_analysis_process(
            record.sequence,
            pid=process.pid,
            process_start_ticks=start_ticks,
            command_digest=str(canonical_digest(tuple(command))),
        )
    except BaseException:
        process.terminate()
        process.wait(timeout=30)
        raise
    running = journal.get(record.sequence)
    assert running is not None
    children[record.sequence] = _AnalysisChild(running, process)
    print(
        json.dumps(
            {
                "event": "focused_continuous_analysis_dispatched",
                "sequence": record.sequence,
                "batch_id": record.batch_id,
                "pid": process.pid,
                "in_flight_analyses": len(children),
            },
            sort_keys=True,
        ),
        flush=True,
    )


def _reap(
    children: dict[int, _AnalysisChild],
    journal: SQLiteFocusedContinuousJournalV0_1,
) -> bool:
    healthy = True
    for sequence, child in tuple(children.items()):
        result = child.process.poll()
        if result is None:
            continue
        if result == 0:
            if _completion_matches(child.record):
                journal.transition(sequence, "analysis_running", "complete")
                event = "focused_continuous_analysis_complete"
            else:
                journal.transition(
                    sequence,
                    "analysis_running",
                    "failed",
                    error="analysis-completion-evidence-missing-or-invalid",
                )
                event = "focused_continuous_analysis_failed"
                healthy = False
        else:
            journal.transition(
                sequence,
                "analysis_running",
                "failed",
                error=f"analysis-exit-{result}",
            )
            event = "focused_continuous_analysis_failed"
            healthy = False
        print(
            json.dumps(
                {
                    "event": event,
                    "sequence": sequence,
                    "batch_id": child.record.batch_id,
                    "exit_status": result,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        del children[sequence]
    return healthy


def _recover(
    args: argparse.Namespace,
    journal: SQLiteFocusedContinuousJournalV0_1,
    children: dict[int, _AnalysisChild],
) -> bool:
    for record in journal.incomplete():
        completion = _completion_state(record)
        if completion == "valid":
            journal.transition(record.sequence, record.state, "complete")
            continue
        if completion == "invalid":
            journal.transition(
                record.sequence,
                record.state,
                "failed",
                error="analysis-completion-evidence-invalid",
            )
            return False
        if record.state == "planned":
            if not _capture_closed(record):
                journal.transition(
                    record.sequence,
                    "planned",
                    "failed",
                    error="capture-outcome-uncertain-after-restart",
                )
                return False
            journal.transition(record.sequence, "planned", "captured")
            recovered = journal.get(record.sequence)
            assert recovered is not None
            _dispatch(args, recovered, journal, children)
        elif record.state == "captured":
            _dispatch(args, record, journal, children)
        elif record.state == "analysis_running":
            expected_command = str(
                canonical_digest(tuple(_analysis_command(args, record)))
            )
            if (
                record.analysis_pid is not None
                and record.analysis_process_start_ticks is not None
                and record.analysis_command_digest == expected_command
                and _pid_start_ticks(record.analysis_pid)
                == record.analysis_process_start_ticks
            ):
                children[record.sequence] = _AnalysisChild(
                    record,
                    _RecoveredProcess(
                        record.analysis_pid,
                        record.analysis_process_start_ticks,
                        record,
                    ),
                )
                continue
            if record.analysis_command_digest not in (None, expected_command):
                journal.transition(
                    record.sequence,
                    "analysis_running",
                    "failed",
                    error="analysis-process-identity-conflict",
                )
                return False
            journal.abandon_analysis_process(record.sequence)
            captured = journal.get(record.sequence)
            assert captured is not None
            _dispatch(args, captured, journal, children)
        else:
            return False
    return True


def _completion_path(record: FocusedContinuousRecordV0_1) -> Path:
    return record.state_root / "analysis-completion.v0.1.json"


def _completion_state(record: FocusedContinuousRecordV0_1) -> str:
    path = _completion_path(record)
    if not path.exists():
        return "absent"
    try:
        completion = decode_focused_analysis_completion(path.read_bytes())
        snapshot = SQLiteCaptureBatchStateStore(
            record.state_root / "capture-batches.sqlite3"
        ).get(CaptureBatchId(record.batch_id))
    except (OSError, ValueError):
        return "invalid"
    if snapshot is None or len(snapshot.successful_recordings) != 2:
        return "invalid"
    ordered = tuple(
        sorted(snapshot.successful_recordings, key=lambda item: str(item.recording_id))
    )
    expected_ids = tuple(item.recording_id for item in ordered)
    expected_digests = tuple(
        item.recording_object.identity_digest() for item in ordered
    )
    return (
        "valid"
        if str(completion.batch_id) == record.batch_id
        and str(completion.capture_definition_digest) == record.definition_digest
        and completion.recording_ids == expected_ids
        and completion.recording_identity_digests == expected_digests
        else "invalid"
    )


def _completion_matches(record: FocusedContinuousRecordV0_1) -> bool:
    return _completion_state(record) == "valid"


def _pid_start_ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        _identity, separator, tail = raw.rpartition(") ")
        if not separator:
            return None
        # Linux proc_pid_stat(5): tail begins at field 3 (state), so field 22
        # (process start ticks) is zero-based tail index 19.
        value = int(tail.split()[19])
    except (OSError, ValueError, IndexError):
        return None
    return value if value > 0 else None


def _write_failure_latch(path: Path, reason: str) -> None:
    payload = (
        json.dumps(
            {"event": "focused_continuous_halted", "reason": reason},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == "__main__":
    raise SystemExit(main())
