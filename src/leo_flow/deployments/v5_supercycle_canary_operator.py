"""Bounded two-phase operator for the isolated 36-slot canary."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, TextIO

from leo_flow.adapters.supercycle_canary_sqlite import SQLiteSupercycleCanaryJournal
from leo_flow.capture.campaign import (
    CampaignAnalysisPort,
    CampaignAnalysisReceipt,
    CampaignCapacityPort,
    CampaignCapturePort,
    CampaignUnit,
)
from leo_flow.capture.supercycle_canary import (
    CANARY_ANALYSIS_TRANSITION_LIMIT,
    CANARY_CAPTURE_TRANSITION_LIMIT,
    CANARY_RECORDINGS,
    CanaryPhase,
    CanaryState,
    SupercycleCanaryCoordinator,
    SupercycleCanaryDefinition,
    build_canary_receipt,
)
from leo_flow.capture.supercycle_canary_codec import (
    decode_canary_definition,
    encode_canary_receipt,
)
from leo_flow.capture.v5_station import (
    V5CaptureStation,
    load_v5_capture_station,
    require_disjoint_station_pair,
    require_passive_both_tx_station_pair,
)
from leo_flow.contracts.capture_batch import CaptureBatchSnapshot
from leo_flow.contracts.core import Digest, UtcNs
from leo_flow.contracts.deferred_analysis import DeferredAnalysisWindowV1
from leo_flow.deployments.supercycle_canary_analysis import (
    SupercycleCanaryStagedAnalysis,
)


class CaptureRuntimeBuilder(Protocol):
    def __call__(
        self,
        definition: SupercycleCanaryDefinition,
        station_a: V5CaptureStation,
        station_b: V5CaptureStation,
        state_root: Path,
    ) -> tuple[CampaignCapturePort, CampaignCapacityPort]: ...


class AnalysisRuntimeBuilder(Protocol):
    def __call__(
        self,
        definition: SupercycleCanaryDefinition,
        state_root: Path,
    ) -> tuple[CampaignAnalysisPort, CampaignCapacityPort]: ...


class _ForbiddenCapture:
    def capture(
        self,
        unit: CampaignUnit,
        *,
        not_before_utc_ns: UtcNs,
        deadline_utc_ns: UtcNs,
    ) -> CaptureBatchSnapshot:
        raise RuntimeError("analysis service has no radio capture authority")


class StagedBuilder(Protocol):
    def __call__(
        self,
        definition: SupercycleCanaryDefinition,
        coordinator: SupercycleCanaryCoordinator,
    ) -> SupercycleCanaryStagedAnalysis: ...


class ClosureReader(Protocol):
    def counts(
        self, definition_digest: str, window: DeferredAnalysisWindowV1
    ) -> tuple[int, int, int, int]: ...


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="leo-v5-supercycle-canary")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "capture-run", "drain-analysis"):
        command = commands.add_parser(name)
        command.add_argument("--definition", type=Path, required=True)
        command.add_argument("--journal", type=Path, required=True)
        if name != "status":
            command.add_argument("--station-a", type=Path, required=True)
            command.add_argument("--station-b", type=Path, required=True)
            command.add_argument("--qualification-receipt", type=Path, required=True)
            command.add_argument("--canary-state-root", type=Path, required=True)
            command.add_argument("--capacity-margin-bytes", type=int, required=True)
            command.add_argument("--maximum-transitions", type=int, required=True)
            command.add_argument("--arm", action="store_true")
            command.add_argument("--confirm-definition-digest", required=True)
        if name == "drain-analysis":
            command.add_argument("--analysis-deadline-seconds", type=int, required=True)
            command.add_argument("--receipt-output", type=Path, required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    capture_runtime_builder: CaptureRuntimeBuilder | None = None,
    analysis_runtime_builder: AnalysisRuntimeBuilder | None = None,
    staged_builder: StagedBuilder | None = None,
    closure_reader: ClosureReader | None = None,
    now_utc_ns: Callable[[], int] = time.time_ns,
    delay: Callable[[float], None] = time.sleep,
) -> int:
    args = _parser().parse_args(argv)
    try:
        definition = decode_canary_definition(args.definition.read_bytes())
        journal = SQLiteSupercycleCanaryJournal(args.journal)
        if args.command == "status":
            state = journal.load(definition)
            return _emit(stdout, "canary_status", definition, state)
        station_a, station_b = _load_armed(args, definition)
        if args.command == "capture-run":
            if capture_runtime_builder is None:
                raise ValueError("canary capture composition is unavailable")
            capture, capacity = capture_runtime_builder(
                definition, station_a, station_b, args.canary_state_root
            )
            analysis: CampaignAnalysisPort = _ForbiddenAnalysis()
        else:
            if analysis_runtime_builder is None:
                raise ValueError("canary analysis composition is unavailable")
            analysis, capacity = analysis_runtime_builder(
                definition, args.canary_state_root
            )
            capture = _ForbiddenCapture()
        coordinator = SupercycleCanaryCoordinator(
            definition,
            journal,
            capture,
            analysis,
            capacity,
            capacity_margin_bytes=args.capacity_margin_bytes,
        )
        if args.command == "capture-run":
            if args.maximum_transitions != CANARY_CAPTURE_TRANSITION_LIMIT:
                raise ValueError("canary capture transition bound differs")
            for _ in range(CANARY_CAPTURE_TRANSITION_LIMIT):
                result = coordinator.capture_next(UtcNs(now_utc_ns()))
                if result.state.phase is CanaryPhase.ANALYZING:
                    return _emit(
                        stdout, "canary_capture_complete", definition, result.state
                    )
                if result.state.phase is CanaryPhase.HALTED:
                    return _emit(
                        stdout, "canary_halted", definition, result.state, code=4
                    )
                if result.status.value == "not_due" and result.unit is not None:
                    wait_ns = (
                        int(result.unit.requested_start_utc_ns)
                        - 15_000_000_000
                        - now_utc_ns()
                    )
                    if wait_ns > 0:
                        delay(wait_ns / 1_000_000_000)
            raise RuntimeError("canary capture transition bound exhausted")
        if (
            args.maximum_transitions != CANARY_ANALYSIS_TRANSITION_LIMIT
            or staged_builder is None
            or closure_reader is None
            or not 1 <= args.analysis_deadline_seconds <= 86_400
        ):
            raise ValueError("canary analysis configuration differs")
        deadline = UtcNs(now_utc_ns() + args.analysis_deadline_seconds * 1_000_000_000)
        staged = staged_builder(definition, coordinator)
        staged_run = staged.run(deadline_utc_ns=deadline)
        for _ in range(CANARY_ANALYSIS_TRANSITION_LIMIT):
            result = coordinator.reconcile_next(deadline_utc_ns=deadline)
            if result.state.phase is CanaryPhase.COMPLETE:
                break
            if result.state.phase is CanaryPhase.HALTED:
                return _emit(stdout, "canary_halted", definition, result.state, code=4)
        state = journal.load(definition)
        if state.phase is not CanaryPhase.COMPLETE:
            raise RuntimeError("canary analysis transition bound exhausted")
        counts = closure_reader.counts(str(definition.digest), staged_run.window)
        if counts != (CANARY_RECORDINGS,) * 4:
            raise RuntimeError("canary dashboard/product closure differs")
        receipt = build_canary_receipt(
            definition,
            state,
            issued_utc_ns=UtcNs(now_utc_ns()),
            benchmarks=staged_run.benchmarks,
            feature_set_count=counts[0],
            waterfall_count=counts[1],
            starlink_suite_terminal_count=counts[2],
            dashboard_recording_count=counts[3],
        )
        _write_new(args.receipt_output, encode_canary_receipt(receipt))
        return _emit(
            stdout, "canary_complete", definition, state, receipt=str(receipt.digest)
        )
    except Exception:  # noqa: BLE001 - sanitized operator boundary
        print('{"event":"canary_transition_failed"}', file=stderr)
        return 4


class _ForbiddenAnalysis:
    def analyze(
        self, snapshot: CaptureBatchSnapshot, *, deadline_utc_ns: UtcNs
    ) -> CampaignAnalysisReceipt:
        raise RuntimeError("capture service has no analysis authority")


def _load_armed(
    args: argparse.Namespace, definition: SupercycleCanaryDefinition
) -> tuple[V5CaptureStation, V5CaptureStation]:
    receipt_bytes = args.qualification_receipt.read_bytes()
    if (
        not args.arm
        or args.confirm_definition_digest != str(definition.digest)
        or str(definition.qualification_receipt_digest)
        != str(Digest.sha256(receipt_bytes))
    ):
        raise ValueError("canary arm evidence differs")
    paths = (args.journal, args.canary_state_root)
    if any(
        not path.is_absolute()
        or ".." in path.parts
        or "continuous" in path.parts
        or "qualification" in path.parts
        or "canary-supercycles" not in path.parts
        for path in paths
    ):
        raise ValueError("canary paths are not isolated")
    first = load_v5_capture_station(args.station_a)
    second = load_v5_capture_station(args.station_b)
    require_disjoint_station_pair(first, second)
    require_passive_both_tx_station_pair(first, second)
    if (
        first.radio.radio_id != definition.radio_a_id
        or second.radio.radio_id != definition.radio_b_id
        or first.specification_digest != definition.station_a_digest
        or second.specification_digest != definition.station_b_digest
    ):
        raise ValueError("canary station identities differ")
    return first, second


def _write_new(path: Path, payload: bytes) -> None:
    if not path.is_absolute() or ".." in path.parts or path.exists():
        raise ValueError("canary receipt output must be a fresh absolute path")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _emit(
    stream: TextIO,
    event: str,
    definition: SupercycleCanaryDefinition,
    state: CanaryState,
    *,
    code: int = 0,
    receipt: str | None = None,
) -> int:
    print(
        json.dumps(
            {
                "event": event,
                "canary_id": definition.canary_id,
                "definition_digest": str(definition.digest),
                "phase": state.phase.value,
                "captured_count": state.captured_count,
                "analyzed_count": state.analyzed_count,
                "halt_reason": state.halt_reason.value if state.halt_reason else None,
                "main_campaign_authorized": False,
                "receipt_digest": receipt,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        file=stream,
    )
    return code
