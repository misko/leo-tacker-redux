"""Restart-safe machine-readable operator entry point for two V5 radios."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from enum import IntEnum
from pathlib import Path
from typing import Any, Protocol, TextIO

from leo_flow.adapters.capture_batch_sqlite import SQLiteCaptureBatchStateStore
from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.application.capture_admission import CaptureAdmissionGate
from leo_flow.application.capture_batch_dashboard import (
    CaptureBatchDashboardProjectionWriter,
    CaptureBatchDashboardPublisher,
)
from leo_flow.application.capture_batches import (
    CaptureBatchCoordinator,
    CaptureBatchStateStore,
)
from leo_flow.capture.batch_serialization import (
    decode_batch_definition,
    encode_batch_definition,
    encode_batch_snapshot,
)
from leo_flow.capture.dual import (
    CaptureAttemptControl,
    CaptureAttemptFailureReason,
    CaptureAttemptRunner,
    CaptureAttemptRunResult,
    DualCaptureExecutor,
)
from leo_flow.capture.spool import SQLiteLocalSpool
from leo_flow.capture.v5_station import (
    V5CaptureStation,
    load_v5_capture_station,
    require_disjoint_station_pair,
)
from leo_flow.contracts.capture_batch import (
    CaptureAttemptState,
    CaptureBatchDefinition,
    CaptureBatchMode,
    ExpectedCaptureAttempt,
)
from leo_flow.contracts.core import (
    CaptureAttemptId,
    CaptureBatchId,
    Digest,
    RadioId,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.deployments.process_isolated_capture import (
    IsolatedAttemptPhaseFailure,
    IsolatedAttemptWork,
    IsolatedAttemptWorkFactory,
    SpawnIsolatedAttemptRunner,
    SpawnProcessSupervisor,
)
from leo_flow.deployments.process_mode_lock import ExclusiveModeLock
from leo_flow.deployments.v5_canary import V5PlanCyclePhase
from leo_flow.deployments.v5_scan import build_station_capture_cycle
from leo_flow.storage.filesystem import FileSystemBlobReader
from leo_flow.storage.recording_codec import SigMFRecordingObjectReader

CATALOG_CREDENTIAL_NAME = "catalog-dsn"


class ExitCode(IntEnum):
    OK = 0
    USAGE_OR_CONFIG = 2
    ARM_REJECTED = 3
    CAPTURE_FAILED = 4
    PUBLICATION_FAILED = 5


class _Cycle(Protocol):
    def preflight(
        self, phase_observer: Callable[[V5PlanCyclePhase], None] | None = None
    ) -> None: ...

    def capture_and_publish_once(
        self, phase_observer: Callable[[V5PlanCyclePhase], None] | None = None
    ) -> bool: ...

    def prepare_first_segment(self) -> None: ...

    def close(self, timeout_s: float) -> None: ...


class PublishedAttemptResolver(Protocol):
    def resolve(self, attempt: ExpectedCaptureAttempt) -> CaptureAttemptRunResult: ...


class _CredentialProvider(Protocol):
    def resolve(self, name: str) -> str: ...


class _ModeLock(Protocol):
    def acquire(self) -> None: ...

    def release(self) -> None: ...


class OneShotCycleAttemptRunner:
    """Preflight before readiness, capture after release, then resolve evidence."""

    def __init__(self, cycle: _Cycle, resolver: PublishedAttemptResolver) -> None:
        self._cycle = cycle
        self._resolver = resolver

    def run(
        self, attempt: ExpectedCaptureAttempt, control: CaptureAttemptControl
    ) -> CaptureAttemptRunResult:
        failure: BaseException | None = None
        result: CaptureAttemptRunResult | None = None
        try:
            self._cycle.preflight()
            if not control.ready_and_wait_for_release():
                raise RuntimeError("capture cancelled before release")
            self._cycle.capture_and_publish_once()
            result = self._resolver.resolve(attempt)
        except BaseException as error:  # noqa: BLE001 - close before sanitization
            failure = error
        try:
            self._cycle.close(10.0)
        except BaseException as error:  # noqa: BLE001 - close failure is terminal
            failure = error
        if failure is not None:
            raise RuntimeError("station capture attempt failed") from failure
        if result is None:  # pragma: no cover - exhaustive defensive check
            raise RuntimeError("station capture produced no result")
        return result


class _PublishedResolver:
    def __init__(
        self, station: V5CaptureStation, dsn: str, batch_id: CaptureBatchId
    ) -> None:
        self._station = station
        self._dsn = dsn
        self._batch_id = batch_id
        self._spool: SQLiteLocalSpool | None = None
        self._catalog: Any | None = None
        self._reader: SigMFRecordingObjectReader | None = None

    def preflight(self, phase_observer: Callable[[V5PlanCyclePhase], None]) -> None:
        station = self._station
        phase_observer(V5PlanCyclePhase.HOST_SPOOL_PREFLIGHT)
        self._spool = SQLiteLocalSpool(
            station.state.spool_database, station.state.recording_root
        )
        self._reader = SigMFRecordingObjectReader(
            FileSystemBlobReader(station.state.cas_root)
        )
        phase_observer(V5PlanCyclePhase.CATALOG_PREFLIGHT)
        self._catalog = _catalog(self._dsn)

    def resolve(self, attempt: ExpectedCaptureAttempt) -> CaptureAttemptRunResult:
        station = self._station
        spool, catalog, reader = self._ready()
        if (
            attempt.radio_id != station.radio.radio_id
            or attempt.plan_id != station.plan.plan_id
        ):
            raise RuntimeError("attempt is routed to another station")
        receipt = spool.durable_recording_for_plan(attempt.plan_id)
        if receipt is None:
            raise RuntimeError("durable spool recording is unavailable")
        published = catalog.get(receipt.recording_id)
        if published is None:
            raise RuntimeError("exact published recording is unavailable")
        observed, completed = self._verified_first_sample(reader, published, attempt)
        return CaptureAttemptRunResult(
            SchemaRef(CaptureAttemptRunResult.SCHEMA_ID),
            self._batch_id,
            attempt.attempt_id,
            attempt.radio_id,
            attempt.plan_id,
            observed,
            completed,
            published,
        )

    def _verified_first_sample(
        self,
        reader: SigMFRecordingObjectReader,
        published: PublishedRecordingRef,
        attempt: ExpectedCaptureAttempt,
    ) -> tuple[UtcNs, UtcNs]:
        with reader.open(published.recording_object) as view:
            manifest = view.manifest
            if (
                manifest.radio_id != attempt.radio_id
                or manifest.plan_id != attempt.plan_id
                or not manifest.segments
            ):
                raise RuntimeError("cataloged recording identity differs")
            observed_starts: list[int] = []
            for segment in manifest.segments:
                continuity = view.continuity(segment.segment_id)
                if (
                    continuity is None
                    or not continuity.is_verified
                    or not continuity.refills
                ):
                    raise RuntimeError("recording continuity is not verified")
                observed_starts.append(continuity.refills[0].utc_start_ns)
            return UtcNs(min(observed_starts)), manifest.capture_finished_utc_ns

    def _ready(self) -> tuple[SQLiteLocalSpool, Any, SigMFRecordingObjectReader]:
        if self._spool is None or self._catalog is None or self._reader is None:
            raise RuntimeError("published resolver preflight is incomplete")
        return self._spool, self._catalog, self._reader


class _StationAttemptWork(IsolatedAttemptWork):
    """Exact station cycle constructed and owned wholly by one spawn child."""

    def __init__(
        self, station: V5CaptureStation, credential: str, batch_id: CaptureBatchId
    ) -> None:
        self._cycle = build_station_capture_cycle(station, credential)
        self._station = station
        self._credential = credential
        self._batch_id = batch_id
        self._resolver: _PublishedResolver | None = None

    def preflight(self) -> None:
        reason = CaptureAttemptFailureReason.CYCLE_PREFLIGHT

        def observe(phase: V5PlanCyclePhase) -> None:
            nonlocal reason
            reason = _PREFLIGHT_FAILURES[phase]

        try:
            self._cycle.preflight(observe)
            resolver = _PublishedResolver(
                self._station, self._credential, self._batch_id
            )
            resolver.preflight(observe)
            self._resolver = resolver
        except BaseException as error:
            raise IsolatedAttemptPhaseFailure(reason) from error
        try:
            self._cycle.prepare_first_segment()
        except BaseException as error:
            raise IsolatedAttemptPhaseFailure(
                CaptureAttemptFailureReason.FIRST_SEGMENT_CONFIGURATION
            ) from error

    def capture(self, attempt: ExpectedCaptureAttempt) -> CaptureAttemptRunResult:
        reason = CaptureAttemptFailureReason.CAPTURE_ENGINE

        def observe(phase: V5PlanCyclePhase) -> None:
            nonlocal reason
            reason = _CAPTURE_FAILURES[phase]

        try:
            self._cycle.capture_and_publish_once(observe)
        except BaseException as error:
            raise IsolatedAttemptPhaseFailure(reason) from error
        try:
            if self._resolver is None:
                raise RuntimeError("published resolver preflight is incomplete")
            return self._resolver.resolve(attempt)
        except BaseException as error:
            raise IsolatedAttemptPhaseFailure(
                CaptureAttemptFailureReason.RECORDING_RESOLUTION
            ) from error

    def close(self, timeout_s: float) -> None:
        self._cycle.close(timeout_s)


_PREFLIGHT_FAILURES = {
    V5PlanCyclePhase.CYCLE_PREFLIGHT: CaptureAttemptFailureReason.CYCLE_PREFLIGHT,
    V5PlanCyclePhase.HOST_SPOOL_PREFLIGHT: (
        CaptureAttemptFailureReason.HOST_SPOOL_PREFLIGHT
    ),
    V5PlanCyclePhase.CATALOG_PREFLIGHT: (CaptureAttemptFailureReason.CATALOG_PREFLIGHT),
    V5PlanCyclePhase.RADIO_ATTESTATION: (CaptureAttemptFailureReason.RADIO_ATTESTATION),
}
_CAPTURE_FAILURES = {
    V5PlanCyclePhase.CAPTURE_ENGINE: CaptureAttemptFailureReason.CAPTURE_ENGINE,
    V5PlanCyclePhase.RECORDING_PUBLICATION: (
        CaptureAttemptFailureReason.RECORDING_PUBLICATION
    ),
}


class _StationAttemptWorkFactory(IsolatedAttemptWorkFactory):
    """Stateless spawn-safe production work factory."""

    def build(
        self,
        station: V5CaptureStation,
        catalog_credential: str,
        batch_id: CaptureBatchId,
    ) -> IsolatedAttemptWork:
        return _StationAttemptWork(station, catalog_credential, batch_id)


def _build_process_isolated_runner(
    station: V5CaptureStation,
    credential: str,
    batch_id: CaptureBatchId,
    supervisor: SpawnProcessSupervisor,
    *,
    post_release_dispatch_delay_s: float = 0.0,
) -> CaptureAttemptRunner:
    return SpawnIsolatedAttemptRunner(
        station,
        credential,
        batch_id,
        _StationAttemptWorkFactory(),
        supervisor=supervisor,
        post_release_dispatch_delay_s=post_release_dispatch_delay_s,
    )


RunnerBuilder = Callable[[V5CaptureStation, str, CaptureBatchId], CaptureAttemptRunner]
StoreFactory = Callable[[Path], CaptureBatchStateStore]
CredentialFactory = Callable[[Path], _CredentialProvider]
StationLoader = Callable[[Path], V5CaptureStation]
BatchLoader = Callable[[Path], CaptureBatchDefinition]
ModeLockFactory = Callable[[Path], _ModeLock]
PublisherBuilder = Callable[[str], CaptureBatchDashboardProjectionWriter]
DrainGateBuilder = Callable[[str], CaptureAdmissionGate]
ProcessSupervisorFactory = Callable[[], SpawnProcessSupervisor]


class _CaptureAdmissionBlocked(RuntimeError):
    pass


def _postgres_drain_gate(dsn: str) -> CaptureAdmissionGate:
    """Load the optional PostgreSQL adapter only for an armed live capture."""

    from leo_flow.adapters.capture_analysis_drain_postgres import (
        PostgresCaptureAnalysisDrainGate,
    )

    return PostgresCaptureAnalysisDrainGate(dsn)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leo-v5-dual-capture",
        description="Inspect, plan, or run one immutable two-radio V5 batch.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        (
            "validate",
            "validate the exact pair and batch without credentials, DB, CAS, or radio",
        ),
        (
            "show-batch",
            "print the exact batch without credentials, DB, CAS, or radio",
        ),
    ):
        child = commands.add_parser(name, help=help_text)
        _inputs(child)
    plan = commands.add_parser(
        "plan-batch",
        aliases=["create-batch"],
        help="exclusively create one canonical independent or coordinated batch",
    )
    plan.add_argument("--station-a", type=Path, required=True)
    plan.add_argument("--station-b", type=Path, required=True)
    plan.add_argument(
        "--mode", choices=[item.value for item in CaptureBatchMode], required=True
    )
    plan.add_argument("--batch-id", required=True)
    plan.add_argument("--attempt-a-id", required=True)
    plan.add_argument("--attempt-b-id", required=True)
    plan.add_argument("--requested-start-a-utc-ns", type=int)
    plan.add_argument("--requested-start-b-utc-ns", type=int)
    plan.add_argument("--common-requested-start-utc-ns", type=int)
    plan.add_argument("--maximum-observed-start-skew-ns", type=int)
    plan.add_argument("--output", type=Path)
    show_state = commands.add_parser(
        "show-state",
        help="export one durable public terminal snapshot without radio contact",
    )
    show_state.add_argument("--batch-database", type=Path, required=True)
    show_state.add_argument("--batch-id", required=True)
    capture = commands.add_parser(
        "capture", help="explicitly arm exactly two process-isolated V5 attempts"
    )
    _inputs(capture)
    capture.add_argument("--arm", action="store_true")
    capture.add_argument("--confirm-analysis-stopped", action="store_true")
    capture.add_argument("--confirm-radio-a-serial", required=True)
    capture.add_argument("--confirm-radio-b-serial", required=True)
    capture.add_argument("--confirm-batch-digest", required=True)
    capture.add_argument("--confirm-pair-digest", required=True)
    capture.add_argument("--credential-directory", type=Path, required=True)
    capture.add_argument("--batch-database", type=Path, required=True)
    return parser


def _inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--station-a", type=Path, required=True)
    parser.add_argument("--station-b", type=Path, required=True)
    parser.add_argument("--batch", type=Path, required=True)


def _show_state(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
    store_factory: StoreFactory,
) -> int:
    try:
        if (
            not args.batch_database.is_absolute()
            or ".." in args.batch_database.parts
            or args.batch_database.is_symlink()
            or not args.batch_database.is_file()
        ):
            raise RuntimeError("capture batch database is unavailable")
        store = store_factory(args.batch_database)
        state = store.get(CaptureBatchId(args.batch_id))
        if state is None:
            raise RuntimeError("capture batch state is unavailable")
        encoded = encode_batch_snapshot(state)
    except Exception:  # noqa: BLE001 - sanitized state handoff boundary
        _emit(stderr, {"event": "dual_state_error"})
        return ExitCode.USAGE_OR_CONFIG
    stdout.write(encoded.decode("utf-8"))
    stdout.flush()
    return ExitCode.OK


def _plan_batch(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
    station_loader: StationLoader,
) -> int:
    try:
        station_a = station_loader(args.station_a)
        station_b = station_loader(args.station_b)
        mode = CaptureBatchMode(args.mode)
        if mode is CaptureBatchMode.INDEPENDENT:
            if (
                args.requested_start_a_utc_ns is None
                or args.requested_start_b_utc_ns is None
                or args.common_requested_start_utc_ns is not None
                or args.maximum_observed_start_skew_ns is not None
            ):
                raise ValueError("independent batch requires two requested starts")
            starts = (
                UtcNs(args.requested_start_a_utc_ns),
                UtcNs(args.requested_start_b_utc_ns),
            )
            maximum_skew = None
        else:
            if (
                args.common_requested_start_utc_ns is None
                or args.requested_start_a_utc_ns is not None
                or args.requested_start_b_utc_ns is not None
                or args.maximum_observed_start_skew_ns is None
            ):
                raise ValueError(
                    "coordinated batch requires one start and a maximum skew"
                )
            common = UtcNs(args.common_requested_start_utc_ns)
            starts = (common, common)
            maximum_skew = args.maximum_observed_start_skew_ns
        definition = CaptureBatchDefinition(
            SchemaRef(CaptureBatchDefinition.SCHEMA_ID),
            CaptureBatchId(args.batch_id),
            mode,
            (
                ExpectedCaptureAttempt(
                    CaptureAttemptId(args.attempt_a_id),
                    station_a.radio.radio_id,
                    station_a.plan.plan_id,
                    starts[0],
                ),
                ExpectedCaptureAttempt(
                    CaptureAttemptId(args.attempt_b_id),
                    station_b.radio.radio_id,
                    station_b.plan.plan_id,
                    starts[1],
                ),
            ),
            maximum_skew,
        )
        ordered = _validate_pair(definition, station_a, station_b)
        encoded = encode_batch_definition(definition)
        payload = _summary(
            definition,
            ordered,
            str(canonical_digest(definition)),
            str(_pair_digest(definition, ordered)),
        )
        payload["event"] = "dual_batch_planned"
        payload["batch"] = json.loads(encoded)
        if args.output is not None:
            payload["output"] = str(args.output)
            _write_new(args.output, encoded)
    except Exception:  # noqa: BLE001 - sanitized offline planning boundary
        _emit(stderr, {"event": "dual_batch_plan_error"})
        return ExitCode.USAGE_OR_CONFIG
    _emit(stdout, payload)
    return ExitCode.OK


def _write_new(path: Path, payload: bytes) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("batch output path must be absolute and normalized")
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    parent_descriptor = os.open(path.parent, directory_flags)
    try:
        if not stat.S_ISDIR(os.fstat(parent_descriptor).st_mode):
            raise ValueError("batch output parent must be a real directory")
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


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    runner_builder: RunnerBuilder | None = None,
    store_factory: StoreFactory = SQLiteCaptureBatchStateStore,
    credential_factory: CredentialFactory = SystemdCredentialProvider,
    station_loader: StationLoader = load_v5_capture_station,
    batch_loader: BatchLoader = lambda path: decode_batch_definition(path.read_bytes()),
    mode_lock_factory: ModeLockFactory = ExclusiveModeLock,
    publisher_builder: PublisherBuilder = lambda dsn: _projection_writer(dsn),
    drain_gate_builder: DrainGateBuilder = _postgres_drain_gate,
    process_supervisor_factory: ProcessSupervisorFactory = SpawnProcessSupervisor,
) -> int:
    args = _parser().parse_args(argv)
    if args.command == "show-state":
        return _show_state(args, stdout, stderr, store_factory)
    if args.command in {"plan-batch", "create-batch"}:
        return _plan_batch(args, stdout, stderr, station_loader)
    try:
        station_a = station_loader(args.station_a)
        station_b = station_loader(args.station_b)
        definition = batch_loader(args.batch)
        ordered = _validate_pair(definition, station_a, station_b)
        batch_digest = str(canonical_digest(definition))
        pair_digest = str(_pair_digest(definition, ordered))
    except Exception:  # noqa: BLE001 - sanitized operator boundary
        _emit(stderr, {"event": "dual_configuration_error"})
        return ExitCode.USAGE_OR_CONFIG

    summary = _summary(definition, ordered, batch_digest, pair_digest)
    if args.command in {"validate", "show-batch"}:
        summary["event"] = "dual_configuration_valid"
        if args.command == "show-batch":
            summary["batch"] = json.loads(encode_batch_definition(definition))
        _emit(stdout, summary)
        return ExitCode.OK

    if (
        not args.arm
        or not args.confirm_analysis_stopped
        or args.confirm_radio_a_serial != station_a.radio.expected_serial
        or args.confirm_radio_b_serial != station_b.radio.expected_serial
        or args.confirm_batch_digest != batch_digest
        or args.confirm_pair_digest != pair_digest
        or not args.batch_database.is_absolute()
    ):
        _emit(stderr, {"event": "dual_capture_arm_rejected"})
        return ExitCode.ARM_REJECTED

    mode_lock: _ModeLock | None = None
    state = None
    existing = None
    failed = False
    publication_failed = False
    admission_blocked = False
    process_supervisor: SpawnProcessSupervisor | None = None
    try:
        mode_lock = mode_lock_factory(station_a.state.mode_lock_path)
        mode_lock.acquire()
        store = store_factory(args.batch_database)
        coordinator = CaptureBatchCoordinator(store)
        existing = store.get(definition.batch_id)
        if existing is not None:
            if not existing.terminal:
                raise RuntimeError("partial batch cannot be recaptured")
            state = existing
            credential = None
        else:
            credential = credential_factory(args.credential_directory).resolve(
                CATALOG_CREDENTIAL_NAME
            )
            try:
                admitted = drain_gate_builder(credential).ready()
            except Exception as error:
                raise _CaptureAdmissionBlocked from error
            if not admitted:
                raise _CaptureAdmissionBlocked
            selected_runner_builder: RunnerBuilder
            if runner_builder is None:
                process_supervisor = process_supervisor_factory()

                def default_runner_builder(
                    station: V5CaptureStation,
                    dsn: str,
                    batch_id: CaptureBatchId,
                ) -> CaptureAttemptRunner:
                    assert process_supervisor is not None
                    return _build_process_isolated_runner(
                        station, dsn, batch_id, process_supervisor
                    )

                selected_runner_builder = default_runner_builder
            else:
                selected_runner_builder = runner_builder
            runners: Mapping[RadioId, CaptureAttemptRunner] = {
                station.radio.radio_id: selected_runner_builder(
                    station, credential, definition.batch_id
                )
                for station in ordered
            }
            state = DualCaptureExecutor(
                coordinator,
                startup_timeout_s=30.0,
                finish_timeout_s=900.0,
                cleanup_timeout_s=15.0,
            ).execute(definition, runners)
            if process_supervisor is not None:
                process_supervisor.abort_all()
        if state is None:  # pragma: no cover - defensive assignment invariant
            raise RuntimeError("dual capture produced no terminal state")
        try:
            publication_credential = credential or credential_factory(
                args.credential_directory
            ).resolve(CATALOG_CREDENTIAL_NAME)
            CaptureBatchDashboardPublisher(
                publisher_builder(publication_credential)
            ).publish_initial(state)
        except Exception:  # noqa: BLE001 - terminal SQLite remains the retry point
            publication_failed = True
    except _CaptureAdmissionBlocked:
        admission_blocked = True
    except Exception:  # noqa: BLE001 - never expose DSN, paths, or driver text
        failed = True
    finally:
        if process_supervisor is not None:
            try:
                process_supervisor.abort_all()
            except Exception:  # noqa: BLE001 - sanitized cleanup failure below
                failed = True
        if mode_lock is not None:
            try:
                mode_lock.release()
            except Exception:  # noqa: BLE001 - sanitized cleanup failure below
                failed = True

    if failed:
        _emit(stderr, {"event": "dual_capture_failed"})
        return ExitCode.CAPTURE_FAILED
    if admission_blocked:
        _emit(stderr, {"event": "dual_capture_admission_blocked"})
        return ExitCode.CAPTURE_FAILED
    if state is None:
        _emit(stderr, {"event": "dual_capture_failed"})
        return ExitCode.CAPTURE_FAILED
    if publication_failed:
        _emit(
            stderr,
            {
                "event": "dual_capture_publication_failed",
                "snapshot": json.loads(encode_batch_snapshot(state)),
            },
        )
        return ExitCode.PUBLICATION_FAILED

    payload = {
        "event": "dual_capture_terminal",
        "replay": existing is not None,
        "snapshot": json.loads(encode_batch_snapshot(state)),
    }
    _emit(stdout, payload)
    return (
        ExitCode.OK
        if all(item.state is CaptureAttemptState.SUCCEEDED for item in state.outcomes)
        else ExitCode.CAPTURE_FAILED
    )


def _validate_pair(
    definition: CaptureBatchDefinition,
    first: V5CaptureStation,
    second: V5CaptureStation,
) -> tuple[V5CaptureStation, V5CaptureStation]:
    require_disjoint_station_pair(first, second)
    stations = {item.radio.radio_id: item for item in (first, second)}
    if (
        len(stations) != 2
        or len({first.radio.expected_serial, second.radio.expected_serial}) != 2
    ):
        raise ValueError("dual stations must be distinct")
    first_attempt, second_attempt = definition.expected_attempts
    ordered = (stations[first_attempt.radio_id], stations[second_attempt.radio_id])
    if any(
        station.plan.plan_id != attempt.plan_id
        or station.plan.plan_digest != canonical_digest(station.capture_plan())
        for station, attempt in zip(ordered, definition.expected_attempts, strict=True)
    ):
        raise ValueError("station and batch plans differ")
    return ordered


def _pair_digest(
    definition: CaptureBatchDefinition,
    stations: tuple[V5CaptureStation, V5CaptureStation],
) -> Digest:
    return canonical_digest(
        {
            "schema": "org.leo-flow.v5-dual-arm/v1",
            "batch_digest": str(canonical_digest(definition)),
            "station_spec_digests": [
                str(item.specification_digest) for item in stations
            ],
        }
    )


def _summary(
    definition: CaptureBatchDefinition,
    stations: tuple[V5CaptureStation, V5CaptureStation],
    batch_digest: str,
    pair_digest: str,
) -> dict[str, object]:
    return {
        "batch_id": str(definition.batch_id),
        "mode": definition.mode.value,
        "batch_digest": batch_digest,
        "pair_digest": pair_digest,
        "radios": [
            {
                "radio_id": str(item.radio.radio_id),
                "radio_serial": item.radio.expected_serial,
                "plan_id": str(item.plan.plan_id),
                "station_spec_digest": str(item.specification_digest),
            }
            for item in stations
        ],
    }


def _catalog(dsn: str) -> Any:
    import psycopg
    from psycopg.rows import dict_row

    from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog

    def connect() -> Any:
        connection = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)
        connection.execute("SET ROLE leo_capture")
        return connection

    return PostgresRecordingCatalog(connect)


def _projection_writer(dsn: str) -> CaptureBatchDashboardProjectionWriter:
    """Lazily compose the integration-owned public projection adapter."""

    import psycopg
    from psycopg.rows import dict_row

    from leo_flow.adapters.dashboard_batch_postgres import (
        PostgresCaptureBatchProjectionWriter,
    )

    def connect() -> Any:
        connection = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)
        connection.execute("SET ROLE leo_capture")
        return connection

    return PostgresCaptureBatchProjectionWriter(connect)


def _emit(stream: TextIO, payload: dict[str, object]) -> None:
    stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    stream.flush()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
