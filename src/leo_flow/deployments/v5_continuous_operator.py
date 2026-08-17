"""Bounded operator for capture-first, deferred-analysis Gauss campaigns."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from io import StringIO
from pathlib import Path
from typing import Protocol, TextIO

from leo_flow.adapters.continuous_collection_sqlite import (
    SQLiteContinuousCollectionJournal,
)
from leo_flow.application.capture_admission import CaptureAdmissionGate
from leo_flow.application.deferred_analysis import (
    DeferredAnalysisWindowStatus,
    ExactDeferredAnalysisWindowCoordinatorV1,
    OnlineAnalysisWindowStatus,
    OnlineDeferredAnalysisWindowCoordinatorV1,
)
from leo_flow.capture.campaign import (
    CampaignAnalysisPort,
    CampaignCapacityPort,
    CampaignCapturePort,
    CampaignDefinition,
    CampaignQualificationReceipt,
)
from leo_flow.capture.campaign_codec import (
    decode_campaign_definition,
    decode_qualification_receipt,
)
from leo_flow.capture.continuous import (
    ContinuousCollectionPhase,
    ContinuousCollectionRecordPhase,
    ContinuousCollectionResult,
    ContinuousCollectionState,
    ContinuousCollectionStatus,
    DeferredCampaignCoordinator,
)
from leo_flow.capture.v5_station import (
    V5CaptureStation,
    load_v5_capture_station,
    require_disjoint_station_pair,
    require_passive_both_tx_station_pair,
)
from leo_flow.contracts.core import UtcNs
from leo_flow.contracts.deferred_analysis import (
    DeferredAnalysisCampaignPhase,
    DeferredAnalysisCampaignRecordPhase,
    DeferredAnalysisCampaignRecordV1,
    DeferredAnalysisCampaignStateV1,
    OnlineAnalysisCampaignStateV1,
)
from leo_flow.deployments.process_mode_lock import ExclusiveModeLock


class CaptureBuilder(Protocol):
    def __call__(
        self,
        definition: CampaignDefinition,
        station_a: V5CaptureStation,
        station_b: V5CaptureStation,
        state_root: Path,
    ) -> CampaignCapturePort: ...


class AnalysisBuilder(Protocol):
    def __call__(self, definition: CampaignDefinition) -> CampaignAnalysisPort: ...


class StagedAnalysisBuilder(Protocol):
    def __call__(
        self,
        definition: CampaignDefinition,
        coordinator: DeferredCampaignCoordinator,
        compute_workers: int,
        projection_workers: int,
    ) -> ExactDeferredAnalysisWindowCoordinatorV1: ...


class OnlineAnalysisBuilder(Protocol):
    def __call__(
        self,
        definition: CampaignDefinition,
        compute_workers: int,
        projection_workers: int,
    ) -> OnlineDeferredAnalysisWindowCoordinatorV1: ...


class CapacityBuilder(Protocol):
    def __call__(self, state_root: Path) -> CampaignCapacityPort: ...


class _Lock(Protocol):
    def acquire(self) -> None: ...

    def release(self) -> None: ...


RETRYABLE_SLICE_EXIT_CODE = 75

_ARMED_COMMANDS = {
    "capture-next",
    "close",
    "analyze-next",
    "capture-run",
    "drain-analysis",
    "drain-analysis-staged",
    "drain-analysis-online",
}


def _parser(
    program_name: str, *, show_deployment_runtime_option: bool
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=program_name,
        description=(
            "Advance a durable capture-first campaign with optional exact "
            "terminal-window analysis."
        ),
    )
    if show_deployment_runtime_option:
        parser.add_argument(
            "--runtime-config",
            type=Path,
            metavar="PATH",
            help="strict no-secret Gauss runtime config required by armed commands",
        )
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="show sanitized durable state")
    status.add_argument("--definition", type=Path, required=True)
    status.add_argument("--journal", type=Path, required=True)
    for name, help_text in (
        ("capture-next", "advance at most one coordinated capture transition"),
        ("close", "irreversibly close RF collection and enable analysis"),
        ("analyze-next", "advance at most one deferred batch analysis"),
        ("capture-run", "run only the bounded coordinated capture phase"),
        ("drain-analysis", "drain only a bounded deferred-analysis slice"),
        (
            "drain-analysis-staged",
            "drain balanced deferred-analysis windows with bounded workers",
        ),
        (
            "drain-analysis-online",
            "analyze complete terminal windows while capture remains open",
        ),
    ):
        command = commands.add_parser(name, help=help_text)
        _armed_inputs(command)
        if name == "capture-next":
            command.add_argument("--now-utc-ns", type=int)
        if name in {
            "analyze-next",
            "drain-analysis",
            "drain-analysis-staged",
            "drain-analysis-online",
        }:
            command.add_argument("--analysis-deadline-seconds", type=int, required=True)
        if name in {
            "capture-run",
            "drain-analysis",
            "drain-analysis-staged",
            "drain-analysis-online",
        }:
            command.add_argument("--maximum-transitions", type=int, required=True)
            command.add_argument("--maximum-runtime-seconds", type=int, required=True)
        if name in {"drain-analysis-staged", "drain-analysis-online"}:
            command.add_argument("--window-batches", type=int, required=True)
            command.add_argument("--compute-workers", type=int, required=True)
            command.add_argument("--projection-workers", type=int, required=True)
        if name == "drain-analysis-online":
            command.add_argument("--online-analysis-lock", type=Path, required=True)
    return parser


def _armed_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--definition", type=Path, required=True)
    parser.add_argument("--qualification-receipt", type=Path, required=True)
    parser.add_argument("--station-a", type=Path, required=True)
    parser.add_argument("--station-b", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--campaign-state-root", type=Path, required=True)
    parser.add_argument("--campaign-lock", type=Path, required=True)
    parser.add_argument("--capacity-margin-bytes", type=int, required=True)
    parser.add_argument("--arm", action="store_true")
    parser.add_argument("--confirm-definition-digest", required=True)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    capture_builder: CaptureBuilder | None = None,
    analysis_builder: AnalysisBuilder | None = None,
    staged_analysis_builder: StagedAnalysisBuilder | None = None,
    online_analysis_builder: OnlineAnalysisBuilder | None = None,
    capacity_builder: CapacityBuilder | None = None,
    start_admission: CaptureAdmissionGate | None = None,
    lock_factory: Callable[[Path], _Lock] = ExclusiveModeLock,
    now_utc_ns: Callable[[], int] = time.time_ns,
    monotonic: Callable[[], float] = time.monotonic,
    delay: Callable[[float], None] = time.sleep,
    program_name: str = "python -m leo_flow.deployments.v5_continuous_operator",
    show_deployment_runtime_option: bool = False,
) -> int:
    args = _parser(
        program_name, show_deployment_runtime_option=show_deployment_runtime_option
    ).parse_args(argv)
    if args.command == "status":
        return _status(args, stdout, stderr)
    try:
        definition, receipt, station_a, station_b = _load_inputs(args)
    except Exception:  # noqa: BLE001 - sanitized operator boundary
        _emit(stderr, {"event": "continuous_configuration_error"})
        return 2
    if not _armed(
        args,
        definition,
        station_a,
        capture_builder,
        analysis_builder,
        staged_analysis_builder,
        online_analysis_builder,
        capacity_builder,
        start_admission,
    ):
        _emit(stderr, {"event": "continuous_arm_rejected"})
        return 3
    assert capture_builder is not None
    assert analysis_builder is not None
    assert capacity_builder is not None
    assert start_admission is not None

    lock: _Lock | None = None
    pipeline_lock: _Lock | None = None
    try:
        lock_path = (
            args.online_analysis_lock
            if args.command == "drain-analysis-online"
            else args.campaign_lock
        )
        lock = lock_factory(lock_path)
        lock.acquire()
        if args.command == "drain-analysis-staged":
            pipeline_lock = lock_factory(station_a.state.mode_lock_path)
            pipeline_lock.acquire()
        buffered_stdout = StringIO()
        code = _execute_armed(
            args,
            definition,
            receipt,
            station_a,
            station_b,
            capture_builder,
            analysis_builder,
            staged_analysis_builder,
            online_analysis_builder,
            capacity_builder,
            start_admission,
            buffered_stdout,
            now_utc_ns,
            monotonic,
            delay,
        )
        if pipeline_lock is not None:
            pipeline_lock.release()
            pipeline_lock = None
        lock.release()
        lock = None
        stdout.write(buffered_stdout.getvalue())
        stdout.flush()
        return code
    except Exception:  # noqa: BLE001 - never expose paths, DSNs, or driver text
        if pipeline_lock is not None:
            try:
                pipeline_lock.release()
            except Exception:  # noqa: BLE001
                pipeline_lock = None
        if lock is not None:
            try:
                lock.release()
            except Exception:  # noqa: BLE001 - report one sanitized failure below
                lock = None
        _emit(stderr, {"event": "continuous_transition_failed"})
        return 4


def _execute_armed(
    args: argparse.Namespace,
    definition: CampaignDefinition,
    receipt: CampaignQualificationReceipt,
    station_a: V5CaptureStation,
    station_b: V5CaptureStation,
    capture_builder: CaptureBuilder,
    analysis_builder: AnalysisBuilder,
    staged_analysis_builder: StagedAnalysisBuilder | None,
    online_analysis_builder: OnlineAnalysisBuilder | None,
    capacity_builder: CapacityBuilder,
    start_admission: CaptureAdmissionGate,
    stdout: TextIO,
    now_utc_ns: Callable[[], int],
    monotonic: Callable[[], float],
    delay: Callable[[float], None],
) -> int:
    journal = SQLiteContinuousCollectionJournal(args.journal)
    if args.command == "drain-analysis-online":
        if online_analysis_builder is None:
            raise RuntimeError("online analysis composition is unavailable")
        _validate_staged_bounds(args, definition)
        return _drain_analysis_online(
            args,
            definition,
            journal,
            online_analysis_builder(
                definition, args.compute_workers, args.projection_workers
            ),
            stdout,
            now_utc_ns,
            monotonic,
        )
    coordinator = DeferredCampaignCoordinator(
        definition,
        journal,
        capture_builder(definition, station_a, station_b, args.campaign_state_root),
        analysis_builder(definition),
        capacity_builder(args.campaign_state_root),
        args.capacity_margin_bytes,
        receipt,
    )
    if args.command == "close":
        result = coordinator.close_capture()
        _emit(stdout, _payload(definition, result))
        return 0
    if args.command == "capture-next":
        _require_start_admission(definition, journal, start_admission)
        now = args.now_utc_ns if args.now_utc_ns is not None else now_utc_ns()
        result = coordinator.capture_next(UtcNs(now))
        _emit(stdout, _payload(definition, result))
        return _result_code(result)
    if args.command == "analyze-next":
        _validate_analysis_deadline(args.analysis_deadline_seconds)
        result = coordinator.analyze_next(
            deadline_utc_ns=UtcNs(
                now_utc_ns() + args.analysis_deadline_seconds * 1_000_000_000
            )
        )
        _emit(stdout, _payload(definition, result))
        return _result_code(result)
    if args.command == "capture-run":
        _validate_slice_bounds(
            args, expected_transitions=definition.capture_run_transition_limit
        )
        return _run_capture(
            args,
            definition,
            journal,
            coordinator,
            start_admission,
            stdout,
            now_utc_ns,
            monotonic,
            delay,
        )
    if args.command == "drain-analysis":
        _validate_run_bounds(
            args, expected_transitions=definition.analysis_drain_transition_limit
        )
        return _drain_analysis(
            args,
            definition,
            journal,
            coordinator,
            stdout,
            now_utc_ns,
            monotonic,
        )
    if args.command == "drain-analysis-staged":
        if staged_analysis_builder is None:
            raise RuntimeError("staged analysis composition is unavailable")
        _validate_staged_bounds(args, definition)
        staged = staged_analysis_builder(
            definition,
            coordinator,
            args.compute_workers,
            args.projection_workers,
        )
        return _drain_analysis_staged(
            args,
            definition,
            journal,
            coordinator,
            staged,
            stdout,
            now_utc_ns,
            monotonic,
        )
    raise RuntimeError("continuous command dispatch is incomplete")


def _run_capture(
    args: argparse.Namespace,
    definition: CampaignDefinition,
    journal: SQLiteContinuousCollectionJournal,
    coordinator: DeferredCampaignCoordinator,
    start_admission: CaptureAdmissionGate,
    stdout: TextIO,
    now_utc_ns: Callable[[], int],
    monotonic: Callable[[], float],
    delay: Callable[[float], None],
) -> int:
    """Run capture transitions only; never cross into one analysis invocation."""

    started = monotonic()
    for _ in range(definition.capture_run_transition_limit):
        state = journal.load(definition)
        if state.phase in {
            ContinuousCollectionPhase.ANALYZING,
            ContinuousCollectionPhase.COMPLETE,
        }:
            _emit(
                stdout,
                _state_payload(
                    definition, state, event="continuous_capture_phase_complete"
                ),
            )
            return 0
        if state.phase is ContinuousCollectionPhase.HALTED:
            _emit(
                stdout,
                _state_payload(definition, state, event="continuous_capture_halted"),
            )
            return 4
        remaining = args.maximum_runtime_seconds - (monotonic() - started)
        if remaining <= 0:
            return _retryable_slice(
                stdout, definition, state, event="continuous_capture_slice_pending"
            )
        _require_start_admission(definition, journal, start_admission)
        now = now_utc_ns()
        result = coordinator.capture_next(UtcNs(now))
        _emit(stdout, _payload(definition, result))
        code = _result_code(result)
        if code != 0:
            return code
        if result.status is ContinuousCollectionStatus.CAPTURE_PHASE_CLOSED:
            return 0
        if result.status is ContinuousCollectionStatus.CAPACITY_BLOCKED:
            return RETRYABLE_SLICE_EXIT_CODE
        if result.status is ContinuousCollectionStatus.NOT_DUE:
            if result.unit is None:
                return RETRYABLE_SLICE_EXIT_CODE
            wake = (
                int(result.unit.requested_start_utc_ns) - definition.preflight_lead_ns
            )
            wait_s = max(0.0, (wake - now) / 1_000_000_000)
            remaining = args.maximum_runtime_seconds - (monotonic() - started)
            if wait_s <= 0 or wait_s > remaining:
                return RETRYABLE_SLICE_EXIT_CODE
            delay(wait_s)
    state = journal.load(definition)
    if state.phase is not ContinuousCollectionPhase.CAPTURING:
        return 0
    return _retryable_slice(
        stdout, definition, state, event="continuous_capture_slice_pending"
    )


def _drain_analysis(
    args: argparse.Namespace,
    definition: CampaignDefinition,
    journal: SQLiteContinuousCollectionJournal,
    coordinator: DeferredCampaignCoordinator,
    stdout: TextIO,
    now_utc_ns: Callable[[], int],
    monotonic: Callable[[], float],
) -> int:
    """Run analysis transitions only and distinguish a bounded pending slice."""

    started = monotonic()
    for _ in range(definition.analysis_drain_transition_limit):
        state = journal.load(definition)
        if state.phase is ContinuousCollectionPhase.CAPTURING:
            _emit(
                stdout,
                _state_payload(
                    definition, state, event="continuous_analysis_phase_not_open"
                ),
            )
            return 4
        if state.phase is ContinuousCollectionPhase.COMPLETE:
            _emit(
                stdout,
                _state_payload(definition, state, event="continuous_analysis_complete"),
            )
            return 0
        if state.phase is ContinuousCollectionPhase.HALTED:
            _emit(
                stdout,
                _state_payload(definition, state, event="continuous_analysis_halted"),
            )
            return 4
        remaining = args.maximum_runtime_seconds - (monotonic() - started)
        if remaining < args.analysis_deadline_seconds:
            return _retryable_slice(
                stdout, definition, state, event="continuous_analysis_slice_pending"
            )
        now = now_utc_ns()
        result = coordinator.analyze_next(
            deadline_utc_ns=UtcNs(now + args.analysis_deadline_seconds * 1_000_000_000)
        )
        _emit(stdout, _payload(definition, result))
        code = _result_code(result)
        if code != 0 or result.status is ContinuousCollectionStatus.COMPLETE:
            return code
    state = journal.load(definition)
    if state.phase is ContinuousCollectionPhase.COMPLETE:
        return 0
    return _retryable_slice(
        stdout, definition, state, event="continuous_analysis_slice_pending"
    )


def _drain_analysis_staged(
    args: argparse.Namespace,
    definition: CampaignDefinition,
    journal: SQLiteContinuousCollectionJournal,
    coordinator: DeferredCampaignCoordinator,
    staged: ExactDeferredAnalysisWindowCoordinatorV1,
    stdout: TextIO,
    now_utc_ns: Callable[[], int],
    monotonic: Callable[[], float],
) -> int:
    """Drain exact 36-batch windows and retain serial receipt CAS closure."""

    started = monotonic()
    for _ in range(definition.staged_analysis_drain_transition_limit):
        state = journal.load(definition)
        if state.phase is ContinuousCollectionPhase.CAPTURING:
            _emit(
                stdout,
                _state_payload(
                    definition, state, event="continuous_analysis_phase_not_open"
                ),
            )
            return 4
        if state.phase is ContinuousCollectionPhase.COMPLETE:
            _emit(
                stdout,
                _state_payload(definition, state, event="continuous_analysis_complete"),
            )
            return 0
        if state.phase is ContinuousCollectionPhase.HALTED:
            _emit(
                stdout,
                _state_payload(definition, state, event="continuous_analysis_halted"),
            )
            return 4
        remaining = args.maximum_runtime_seconds - (monotonic() - started)
        if remaining < args.analysis_deadline_seconds:
            return _retryable_slice(
                stdout, definition, state, event="continuous_analysis_slice_pending"
            )
        deadline = UtcNs(now_utc_ns() + args.analysis_deadline_seconds * 1_000_000_000)
        if state.analyzed_count == definition.target_successes:
            result = coordinator.analyze_next(deadline_utc_ns=deadline)
            _emit(stdout, _payload(definition, result))
            return _result_code(result)
        staged_state = DeferredAnalysisCampaignStateV1(
            state.definition_digest,
            DeferredAnalysisCampaignPhase(state.phase.value),
            state.analyzed_count,
            tuple(
                DeferredAnalysisCampaignRecordV1(
                    record.unit.success_index,
                    DeferredAnalysisCampaignRecordPhase(record.phase.value),
                    record.snapshot,
                )
                for record in state.records
                if record.phase
                in {
                    ContinuousCollectionRecordPhase.CAPTURED,
                    ContinuousCollectionRecordPhase.ANALYSIS_FAILED,
                }
            ),
        )
        staged_result = staged.advance_window(staged_state, deadline_utc_ns=deadline)
        _emit(
            stdout,
            {
                "event": "continuous_staged_analysis_window",
                "status": staged_result.status.value,
                "first_success_index": staged_result.first_success_index,
                "reconciled_count": staged_result.reconciled_count,
                "terminal_stage": (
                    None
                    if staged_result.terminal_stage is None
                    else staged_result.terminal_stage.value
                ),
                "parked_count": len(staged_result.parked_ids),
                "window_digest": (
                    None
                    if staged_result.window_digest is None
                    else str(staged_result.window_digest)
                ),
            },
        )
        if staged_result.status is DeferredAnalysisWindowStatus.PARKED:
            halted = coordinator.halt_analysis()
            _emit(stdout, _payload(definition, halted))
            return 4
        if staged_result.status is DeferredAnalysisWindowStatus.PENDING:
            return RETRYABLE_SLICE_EXIT_CODE
    state = journal.load(definition)
    if state.phase is ContinuousCollectionPhase.COMPLETE:
        return 0
    return _retryable_slice(
        stdout, definition, state, event="continuous_analysis_slice_pending"
    )


def _drain_analysis_online(
    args: argparse.Namespace,
    definition: CampaignDefinition,
    journal: SQLiteContinuousCollectionJournal,
    online: OnlineDeferredAnalysisWindowCoordinatorV1,
    stdout: TextIO,
    now_utc_ns: Callable[[], int],
    monotonic: Callable[[], float],
) -> int:
    """Analyze immutable full windows without writing the capture journal."""

    started = monotonic()
    for _ in range(definition.staged_analysis_drain_transition_limit):
        state = journal.load(definition)
        if state.phase is not ContinuousCollectionPhase.CAPTURING:
            _emit(
                stdout,
                _state_payload(
                    definition, state, event="continuous_online_analysis_phase_closed"
                ),
            )
            return 0
        if args.maximum_runtime_seconds - (monotonic() - started) < (
            args.analysis_deadline_seconds
        ):
            return _retryable_slice(
                stdout,
                definition,
                state,
                event="continuous_online_analysis_slice_pending",
            )
        records = tuple(
            DeferredAnalysisCampaignRecordV1(
                record.unit.success_index,
                DeferredAnalysisCampaignRecordPhase(record.phase.value),
                record.snapshot,
            )
            for record in state.records
            if record.phase
            in {
                ContinuousCollectionRecordPhase.CAPTURED,
                ContinuousCollectionRecordPhase.ANALYSIS_FAILED,
            }
        )
        online_state = OnlineAnalysisCampaignStateV1(state.definition_digest, records)
        result = online.advance_available(
            online_state,
            deadline_utc_ns=UtcNs(
                now_utc_ns() + args.analysis_deadline_seconds * 1_000_000_000
            ),
        )
        _emit(
            stdout,
            {
                "event": "continuous_online_analysis_window",
                "status": result.status.value,
                "first_success_index": result.first_success_index,
                "terminal_stage": (
                    None
                    if result.terminal_stage is None
                    else result.terminal_stage.value
                ),
                "parked_count": len(result.parked_ids),
                "window_digest": (
                    None if result.window_digest is None else str(result.window_digest)
                ),
                "capture_revision": state.revision,
                "captured_count": state.captured_count,
            },
        )
        if result.status is OnlineAnalysisWindowStatus.CAUGHT_UP:
            return 0
        if result.status is OnlineAnalysisWindowStatus.PENDING:
            return RETRYABLE_SLICE_EXIT_CODE
        if result.status is OnlineAnalysisWindowStatus.PARKED:
            return 4
    state = journal.load(definition)
    return _retryable_slice(
        stdout,
        definition,
        state,
        event="continuous_online_analysis_slice_pending",
    )


def _retryable_slice(
    stdout: TextIO,
    definition: CampaignDefinition,
    state: ContinuousCollectionState,
    *,
    event: str,
) -> int:
    _emit(stdout, _state_payload(definition, state, event=event))
    return RETRYABLE_SLICE_EXIT_CODE


def _require_start_admission(
    definition: CampaignDefinition,
    journal: SQLiteContinuousCollectionJournal,
    gate: CaptureAdmissionGate,
) -> None:
    if not journal.load(definition).records and not gate.ready():
        raise RuntimeError("continuous collection start drain gate is closed")


def _load_inputs(
    args: argparse.Namespace,
) -> tuple[
    CampaignDefinition,
    CampaignQualificationReceipt,
    V5CaptureStation,
    V5CaptureStation,
]:
    definition = decode_campaign_definition(args.definition.read_bytes())
    receipt = decode_qualification_receipt(args.qualification_receipt.read_bytes())
    first = load_v5_capture_station(args.station_a)
    second = load_v5_capture_station(args.station_b)
    require_disjoint_station_pair(first, second)
    require_passive_both_tx_station_pair(first, second)
    if (
        definition.qualification
        or receipt.digest != definition.qualification_receipt_digest
        or definition.radio_a_id != first.radio.radio_id
        or definition.radio_b_id != second.radio.radio_id
        or definition.station_a_digest != first.specification_digest
        or definition.station_b_digest != second.specification_digest
    ):
        raise ValueError("continuous collection identities differ")
    return definition, receipt, first, second


def _armed(
    args: argparse.Namespace,
    definition: CampaignDefinition,
    station_a: V5CaptureStation,
    capture_builder: CaptureBuilder | None,
    analysis_builder: AnalysisBuilder | None,
    staged_analysis_builder: StagedAnalysisBuilder | None,
    online_analysis_builder: OnlineAnalysisBuilder | None,
    capacity_builder: CapacityBuilder | None,
    start_admission: CaptureAdmissionGate | None,
) -> bool:
    paths = (args.journal, args.campaign_state_root, args.campaign_lock)
    return bool(
        args.arm
        and not definition.analysis_after_each_capture
        and args.confirm_definition_digest == str(definition.digest)
        and capture_builder is not None
        and analysis_builder is not None
        and (
            args.command != "drain-analysis-staged"
            or staged_analysis_builder is not None
        )
        and (
            args.command != "drain-analysis-online"
            or online_analysis_builder is not None
        )
        and capacity_builder is not None
        and start_admission is not None
        and all(path.is_absolute() and ".." not in path.parts for path in paths)
        and args.campaign_lock != station_a.state.mode_lock_path
        and (
            args.command != "drain-analysis-online"
            or (
                args.online_analysis_lock.is_absolute()
                and ".." not in args.online_analysis_lock.parts
                and args.online_analysis_lock != args.campaign_lock
                and args.online_analysis_lock != station_a.state.mode_lock_path
            )
        )
        and isinstance(args.capacity_margin_bytes, int)
        and not isinstance(args.capacity_margin_bytes, bool)
        and args.capacity_margin_bytes >= 0
    )


def _validate_analysis_deadline(value: int) -> None:
    if isinstance(value, bool) or not 1 <= value <= 900:
        raise ValueError("analysis deadline must be within 1..900 seconds")


def _validate_run_bounds(
    args: argparse.Namespace, *, expected_transitions: int
) -> None:
    _validate_analysis_deadline(args.analysis_deadline_seconds)
    _validate_slice_bounds(args, expected_transitions=expected_transitions)


def _validate_staged_bounds(
    args: argparse.Namespace, definition: CampaignDefinition
) -> None:
    _validate_run_bounds(
        args, expected_transitions=definition.staged_analysis_drain_transition_limit
    )
    if (
        args.window_batches != 36
        or isinstance(args.compute_workers, bool)
        or not 1 <= args.compute_workers <= 8
        or isinstance(args.projection_workers, bool)
        or not 1 <= args.projection_workers <= 4
    ):
        raise ValueError("staged analysis bounds are invalid")


def _validate_slice_bounds(
    args: argparse.Namespace, *, expected_transitions: int
) -> None:
    if (
        isinstance(args.maximum_transitions, bool)
        or args.maximum_transitions != expected_transitions
        or isinstance(args.maximum_runtime_seconds, bool)
        or not 1 <= args.maximum_runtime_seconds <= 32_400
    ):
        raise ValueError("continuous run bounds are invalid")


def _status(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    try:
        definition = decode_campaign_definition(args.definition.read_bytes())
        state = SQLiteContinuousCollectionJournal(args.journal).load(definition)
    except Exception:  # noqa: BLE001 - sanitized read-only boundary
        _emit(stderr, {"event": "continuous_status_error"})
        return 2
    _emit(stdout, _state_payload(definition, state, event="continuous_status"))
    return 0


def _payload(
    definition: CampaignDefinition, result: ContinuousCollectionResult
) -> dict[str, object]:
    value = _state_payload(definition, result.state, event="continuous_transition")
    value.update(
        {
            "status": result.status.value,
            "unit_digest": str(result.unit.digest) if result.unit is not None else None,
        }
    )
    return value


def _state_payload(
    definition: CampaignDefinition,
    state: ContinuousCollectionState,
    *,
    event: str,
) -> dict[str, object]:
    return {
        "event": event,
        "campaign_id": definition.campaign_id,
        "definition_digest": str(definition.digest),
        "phase": state.phase.value,
        "halt_reason": state.halt_reason.value if state.halt_reason else None,
        "revision": state.revision,
        "record_count": len(state.records),
        "captured_count": state.captured_count,
        "analyzed_count": state.analyzed_count,
    }


def _result_code(result: ContinuousCollectionResult) -> int:
    return 4 if result.status is ContinuousCollectionStatus.HALTED else 0


def _emit(stream: TextIO, value: dict[str, object]) -> None:
    stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    stream.flush()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
