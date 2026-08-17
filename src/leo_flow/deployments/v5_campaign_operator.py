"""Bounded Python CLI for one finite Gauss V5 campaign transition.

Production capture and analysis adapters are injected by deployment
composition because they cross component ownership.  Without both adapters,
the armed command fails closed; offline validation remains filesystem-read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, TextIO

from leo_flow.adapters.campaign_sqlite import SQLiteCampaignJournal
from leo_flow.capture.campaign import (
    CAMPAIGN_CELLS,
    CAMPAIGN_ROUNDS,
    CAMPAIGN_SUCCESS_TARGET,
    CampaignAnalysisPort,
    CampaignCapacityPort,
    CampaignCapturePort,
    CampaignCoordinator,
    CampaignDefinition,
    CampaignQualificationReceipt,
    CampaignRunResult,
    CampaignRunStatus,
    build_campaign_unit,
    build_qualification_receipt,
    materialize_campaign_station,
    required_remaining_capacity_bytes,
)
from leo_flow.capture.campaign_codec import (
    decode_campaign_definition,
    decode_qualification_receipt,
    encode_campaign_definition,
    encode_qualification_receipt,
)
from leo_flow.capture.v5_station import (
    V5CaptureStation,
    load_v5_capture_station,
    require_passive_both_tx_station_pair,
)
from leo_flow.contracts.core import UtcNs
from leo_flow.deployments.process_mode_lock import ExclusiveModeLock


class CaptureBuilder(Protocol):
    def __call__(
        self,
        definition: CampaignDefinition,
        station_a: V5CaptureStation,
        station_b: V5CaptureStation,
        campaign_state_root: Path,
    ) -> CampaignCapturePort: ...


class AnalysisBuilder(Protocol):
    def __call__(self, definition: CampaignDefinition) -> CampaignAnalysisPort: ...


class CapacityBuilder(Protocol):
    def __call__(self, campaign_state_root: Path) -> CampaignCapacityPort: ...


class _Lock(Protocol):
    def acquire(self) -> None: ...

    def release(self) -> None: ...


def _parser(
    program_name: str, *, show_deployment_runtime_option: bool
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=program_name,
        description="Validate or advance one bounded Gauss coordinated campaign unit.",
    )
    if show_deployment_runtime_option:
        parser.add_argument(
            "--runtime-config",
            metavar="PATH",
            help="strict no-secret deployment config required by run/run-next",
        )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("plan-qualification", "create one fresh immutable 9-cell qualification"),
        ("plan-main", "create one fresh immutable 104-round main campaign"),
    ):
        plan = commands.add_parser(name, help=help_text)
        plan.add_argument("--campaign-id", required=True)
        plan.add_argument("--start-utc-ns", type=int, required=True)
        plan.add_argument("--maximum-start-lateness-ns", type=int, required=True)
        plan.add_argument("--station-a", type=Path, required=True)
        plan.add_argument("--station-b", type=Path, required=True)
        plan.add_argument("--output", type=Path, required=True)
        if name == "plan-main":
            plan.add_argument("--qualification-definition", type=Path, required=True)
            plan.add_argument("--qualification-receipt", type=Path, required=True)
            plan.add_argument(
                "--deferred-analysis",
                action="store_true",
                help="bind this private main definition to capture-first analysis",
            )
    validate = commands.add_parser(
        "validate", help="offline validation without journal, DB, CAS, or radio access"
    )
    _definition_inputs(validate)
    validate.add_argument("--campaign-state-root", type=Path, required=True)
    status = commands.add_parser("status", help="show sanitized durable accounting")
    status.add_argument("--definition", type=Path, required=True)
    status.add_argument("--journal", type=Path, required=True)
    receipt = commands.add_parser(
        "qualification-receipt", help="emit a receipt for one completed 9-cell round"
    )
    receipt.add_argument("--definition", type=Path, required=True)
    receipt.add_argument("--journal", type=Path, required=True)
    receipt.add_argument("--issued-utc-ns", type=int, required=True)
    for name, help_text in (
        ("run-next", "arm and advance at most one capture-to-analysis unit"),
        ("run", "run the finite campaign until a stop gate or completion"),
    ):
        run = commands.add_parser(name, help=help_text)
        _definition_inputs(run)
        run.add_argument("--journal", type=Path, required=True)
        run.add_argument("--campaign-state-root", type=Path, required=True)
        run.add_argument("--campaign-lock", type=Path, required=True)
        if name == "run-next":
            run.add_argument("--now-utc-ns", type=int)
        run.add_argument("--capacity-margin-bytes", type=int, required=True)
        run.add_argument("--arm", action="store_true")
        run.add_argument("--confirm-definition-digest", required=True)
    return parser


def _definition_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--definition", type=Path, required=True)
    parser.add_argument("--qualification-receipt", type=Path)
    parser.add_argument("--station-a", type=Path, required=True)
    parser.add_argument("--station-b", type=Path, required=True)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    capture_builder: CaptureBuilder | None = None,
    analysis_builder: AnalysisBuilder | None = None,
    capacity_builder: CapacityBuilder | None = None,
    lock_factory: Callable[[Path], _Lock] = ExclusiveModeLock,
    now_utc_ns: Callable[[], int] = time.time_ns,
    delay: Callable[[float], None] = time.sleep,
    program_name: str = "python -m leo_flow.deployments.v5_campaign_operator",
    show_deployment_runtime_option: bool = False,
) -> int:
    args = _parser(
        program_name,
        show_deployment_runtime_option=show_deployment_runtime_option,
    ).parse_args(argv)
    if args.command in {"plan-qualification", "plan-main"}:
        return _plan_campaign(args, stdout, stderr)
    if args.command == "status":
        return _status(args, stdout, stderr)
    if args.command == "qualification-receipt":
        return _receipt(args, stdout, stderr)
    try:
        definition, receipt, station_a, station_b = _load_inputs(args)
        _validate_materialization(
            definition, station_a, station_b, args.campaign_state_root
        )
    except Exception:  # noqa: BLE001 - sanitized operator boundary
        _emit(stderr, {"event": "campaign_configuration_error"})
        return 2
    if args.command == "validate":
        _emit(stdout, _summary("campaign_configuration_valid", definition, receipt))
        return 0
    if (
        not args.arm
        or not definition.analysis_after_each_capture
        or args.confirm_definition_digest != str(definition.digest)
        or capture_builder is None
        or analysis_builder is None
        or capacity_builder is None
        or not args.journal.is_absolute()
        or not args.campaign_state_root.is_absolute()
        or not args.campaign_lock.is_absolute()
        or args.campaign_lock == station_a.state.mode_lock_path
        or args.capacity_margin_bytes < 0
    ):
        _emit(stderr, {"event": "campaign_arm_rejected"})
        return 3
    lock: _Lock | None = None
    try:
        lock = lock_factory(args.campaign_lock)
        lock.acquire()
        journal = SQLiteCampaignJournal(args.journal)
        capture = capture_builder(
            definition, station_a, station_b, args.campaign_state_root
        )
        analysis = analysis_builder(definition)
        capacity = capacity_builder(args.campaign_state_root)
        coordinator = CampaignCoordinator(
            definition,
            journal,
            capture,
            analysis,
            capacity,
            args.capacity_margin_bytes,
            receipt,
        )
        wait_cycles = 0
        while True:
            explicit_now = getattr(args, "now_utc_ns", None)
            current_now = explicit_now if explicit_now is not None else now_utc_ns()
            current = journal.load(definition)
            required = required_remaining_capacity_bytes(
                definition, current, margin_bytes=args.capacity_margin_bytes
            )
            result = coordinator.run_next(UtcNs(current_now))
            payload = _transition_payload(definition, receipt, result, required)
            if args.command == "run-next":
                break
            _emit(stdout, payload)
            if result.status is CampaignRunStatus.UNIT_COMPLETE:
                wait_cycles = 0
                continue
            if result.status is CampaignRunStatus.NOT_DUE and result.unit is not None:
                wake_utc_ns = (
                    int(result.unit.requested_start_utc_ns)
                    - definition.preflight_lead_ns
                )
                wait_s = max(0.0, (wake_utc_ns - current_now) / 1e9)
                if wait_s == 0:
                    raise RuntimeError("campaign clock did not advance to preflight")
                wait_cycles += 1
                if wait_cycles > 4:
                    raise RuntimeError("campaign clock did not reach preflight")
                delay(wait_s)
                continue
            break
    except Exception:  # noqa: BLE001 - never expose secrets, paths, or driver text
        _emit(stderr, {"event": "campaign_transition_failed"})
        return 4
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:  # noqa: BLE001 - sanitized lock cleanup boundary
                _emit(stderr, {"event": "campaign_transition_failed"})
                return 4
    if args.command == "run-next":
        _emit(stdout, _transition_payload(definition, receipt, result, required))
    return (
        0
        if result.status
        in {
            CampaignRunStatus.UNIT_COMPLETE,
            CampaignRunStatus.NOT_DUE,
            CampaignRunStatus.CAMPAIGN_COMPLETE,
        }
        else 4
    )


def _transition_payload(
    definition: CampaignDefinition,
    receipt: CampaignQualificationReceipt | None,
    result: CampaignRunResult,
    required: int,
) -> dict[str, object]:
    payload = _summary("campaign_transition", definition, receipt)
    payload.update(
        {
            "status": result.status.value,
            "completed_successes": result.state.completed_successes,
            "accepted_balanced_rounds": result.state.accepted_balanced_rounds,
            "successful_counts": list(result.state.successful_counts),
            "required_remaining_capacity_bytes": required,
            "unit_digest": (
                str(result.unit.digest) if result.unit is not None else None
            ),
        }
    )
    return payload


def _load_inputs(
    args: argparse.Namespace,
) -> tuple[
    CampaignDefinition,
    CampaignQualificationReceipt | None,
    V5CaptureStation,
    V5CaptureStation,
]:
    definition = decode_campaign_definition(args.definition.read_bytes())
    receipt = (
        decode_qualification_receipt(args.qualification_receipt.read_bytes())
        if args.qualification_receipt is not None
        else None
    )
    if definition.qualification:
        if receipt is not None:
            raise ValueError("qualification cannot consume a prior receipt")
    elif receipt is None or receipt.digest != definition.qualification_receipt_digest:
        raise ValueError("main campaign qualification receipt differs")
    first = load_v5_capture_station(args.station_a)
    second = load_v5_capture_station(args.station_b)
    require_passive_both_tx_station_pair(first, second)
    return (
        definition,
        receipt,
        first,
        second,
    )


def _plan_campaign(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    try:
        station_a = load_v5_capture_station(args.station_a)
        station_b = load_v5_capture_station(args.station_b)
        require_passive_both_tx_station_pair(station_a, station_b)
        qualification = args.command == "plan-qualification"
        receipt: CampaignQualificationReceipt | None = None
        if not qualification:
            qualification_definition = decode_campaign_definition(
                args.qualification_definition.read_bytes()
            )
            receipt = decode_qualification_receipt(
                args.qualification_receipt.read_bytes()
            )
            if (
                not qualification_definition.qualification
                or receipt.qualification_definition_digest
                != qualification_definition.digest
                or qualification_definition.radio_a_id != station_a.radio.radio_id
                or qualification_definition.radio_b_id != station_b.radio.radio_id
                or qualification_definition.station_a_digest
                != station_a.specification_digest
                or qualification_definition.station_b_digest
                != station_b.specification_digest
                or args.start_utc_ns <= int(receipt.issued_utc_ns)
            ):
                raise ValueError("qualification does not authorize this main campaign")
        definition = CampaignDefinition(
            args.campaign_id,
            UtcNs(args.start_utc_ns),
            station_a.radio.radio_id,
            station_b.radio.radio_id,
            station_a.specification_digest,
            station_b.specification_digest,
            maximum_start_lateness_ns=args.maximum_start_lateness_ns,
            qualification_receipt_digest=(
                receipt.digest if receipt is not None else None
            ),
            qualification=qualification,
            analysis_after_each_capture=(not getattr(args, "deferred_analysis", False)),
        )
        encoded = encode_campaign_definition(definition)
        _write_new(args.output, encoded)
    except Exception:  # noqa: BLE001 - sanitized offline planning boundary
        _emit(stderr, {"event": "campaign_plan_error"})
        return 2
    payload = _summary("campaign_planned", definition, receipt)
    payload["output"] = str(args.output)
    _emit(stdout, payload)
    return 0


def _write_new(path: Path, payload: bytes) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("campaign output path must be absolute and normalized")
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    parent_descriptor = os.open(path.parent, directory_flags)
    try:
        if not stat.S_ISDIR(os.fstat(parent_descriptor).st_mode):
            raise ValueError("campaign output parent must be a real directory")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_descriptor)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _validate_materialization(
    definition: CampaignDefinition,
    station_a: V5CaptureStation,
    station_b: V5CaptureStation,
    state_root: Path,
) -> None:
    target = 9 if definition.qualification else CAMPAIGN_SUCCESS_TARGET
    seen_plans = set()
    for index in range(target):
        unit = build_campaign_unit(
            definition,
            success_index=index,
            slot_index=index,
            retry_index=0,
            requested_start_utc_ns=UtcNs(
                int(definition.start_utc_ns)
                + index
                * definition.slot_period_numerator_ns
                // definition.slot_period_denominator
            ),
        )
        first = materialize_campaign_station(
            definition, station_a, unit, side="a", campaign_state_root=state_root
        )
        second = materialize_campaign_station(
            definition, station_b, unit, side="b", campaign_state_root=state_root
        )
        for plan in (first.plan, second.plan):
            if plan.plan_id in seen_plans:
                raise ValueError("campaign plan identity is reused")
            seen_plans.add(plan.plan_id)
            if plan.hardware_block_samples != unit.cell.hardware_block_samples:
                raise ValueError("campaign plan has an unexpected metadata block size")
            if plan.sample_count % plan.hardware_block_samples:
                raise ValueError("campaign dwell is not aligned to metadata refills")
    if len(seen_plans) != target * 2:
        raise ValueError("campaign plan inventory is incomplete")


def _status(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    try:
        definition = decode_campaign_definition(args.definition.read_bytes())
        if not args.journal.is_absolute() or not args.journal.is_file():
            raise ValueError("campaign journal is unavailable")
        state = SQLiteCampaignJournal(args.journal).load(definition)
    except Exception:  # noqa: BLE001 - sanitized operator boundary
        _emit(stderr, {"event": "campaign_status_error"})
        return 2
    payload = _summary("campaign_status", definition, None)
    payload.update(
        {
            "completed_successes": state.completed_successes,
            "accepted_balanced_rounds": state.accepted_balanced_rounds,
            "successful_counts": list(state.successful_counts),
            "journal_revision": state.revision,
        }
    )
    _emit(stdout, payload)
    return 0


def _receipt(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    try:
        definition = decode_campaign_definition(args.definition.read_bytes())
        if not args.journal.is_absolute() or not args.journal.is_file():
            raise ValueError("campaign journal is unavailable")
        state = SQLiteCampaignJournal(args.journal).load(definition)
        receipt = build_qualification_receipt(
            definition, state, issued_utc_ns=UtcNs(args.issued_utc_ns)
        )
    except Exception:  # noqa: BLE001 - sanitized operator boundary
        _emit(stderr, {"event": "campaign_qualification_receipt_error"})
        return 2
    stdout.write(encode_qualification_receipt(receipt).decode("utf-8"))
    stdout.flush()
    return 0


def _summary(
    event: str,
    definition: CampaignDefinition,
    receipt: CampaignQualificationReceipt | None,
) -> dict[str, object]:
    return {
        "event": event,
        "campaign_id": definition.campaign_id,
        "campaign_kind": "qualification" if definition.qualification else "main",
        "definition_digest": str(definition.digest),
        "qualification_receipt_digest": (
            str(receipt.digest) if receipt is not None else None
        ),
        "target_successes": definition.target_successes,
        "target_per_cell": 1 if definition.qualification else CAMPAIGN_ROUNDS,
        "cells": [item.document() for item in CAMPAIGN_CELLS],
    }


def _emit(stream: TextIO, payload: dict[str, object]) -> None:
    stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    stream.flush()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
