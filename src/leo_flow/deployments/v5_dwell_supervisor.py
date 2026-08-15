"""Capture-only supervisor around the durable V5 dwell scheduler."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from leo_flow.capture.clock import CaptureClock
from leo_flow.capture.publication import PublicationReconciler, ReconciliationResult
from leo_flow.capture.spool import SQLiteLocalSpool
from leo_flow.contracts.core import (
    Digest,
    DigestAlgorithm,
    PlanId,
    RecordingId,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.dwell import DwellRequest
from leo_flow.maintenance.capacity import (
    CapacityConfiguration,
    check_capacity,
    exit_code,
)
from leo_flow.services.lifecycle import ServiceLoop
from leo_flow.storage.local_recording import (
    LocalRecordingNotFinalizedError,
    RootedSigMFRecordingStore,
)

from .v5_dwell_request import DwellCaptureReceipt, OneShotDwellCaptureScheduler


class _HostGuard(Protocol):
    def acquire(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class ClockAttestation:
    source: str
    synchronized: bool
    observed_utc_ns: UtcNs
    valid_until_utc_ns: UtcNs
    uncertainty_ns: int

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("clock attestation source cannot be empty")
        if self.observed_utc_ns < 0 or self.valid_until_utc_ns < self.observed_utc_ns:
            raise ValueError("clock attestation interval is invalid")
        if self.uncertainty_ns < 0:
            raise ValueError("clock uncertainty cannot be negative")


class TrustedCaptureClock:
    """CaptureClock that fails every UTC read without fresh synchronization proof."""

    def __init__(
        self,
        clock: CaptureClock,
        attestation: Callable[[], ClockAttestation],
        *,
        maximum_uncertainty_ns: int,
    ) -> None:
        if maximum_uncertainty_ns < 0:
            raise ValueError("maximum clock uncertainty cannot be negative")
        self._clock = clock
        self._attestation = attestation
        self._maximum_uncertainty_ns = maximum_uncertainty_ns
        self._last_utc_ns: int | None = None
        self._lock = threading.Lock()
        self._last_attestation: ClockAttestation | None = None

    @property
    def last_attestation(self) -> ClockAttestation | None:
        return self._last_attestation

    def now_utc_ns(self) -> int:
        now = self._clock.now_utc_ns()
        proof = self._attestation()
        if (
            not proof.synchronized
            or proof.uncertainty_ns > self._maximum_uncertainty_ns
            or not proof.observed_utc_ns <= now <= proof.valid_until_utc_ns
        ):
            raise SupervisorPreflightError("trusted clock attestation failed")
        with self._lock:
            if self._last_utc_ns is not None and now < self._last_utc_ns:
                raise SupervisorPreflightError("trusted UTC clock moved backwards")
            self._last_utc_ns = now
            self._last_attestation = proof
        return now

    def now_monotonic_ns(self) -> int:
        return self._clock.now_monotonic_ns()


@dataclass(frozen=True)
class RetentionCapacityPolicy:
    maximum_retained_recordings: int
    maximum_retained_bytes: int
    metadata_reserve_bytes_per_recording: int
    bytes_per_paired_sample: int = 8

    def __post_init__(self) -> None:
        if (
            min(
                self.maximum_retained_recordings,
                self.maximum_retained_bytes,
                self.metadata_reserve_bytes_per_recording,
                self.bytes_per_paired_sample,
            )
            <= 0
        ):
            raise ValueError("retention and capacity bounds must be positive")


@dataclass(frozen=True)
class DurableDwellReceipt:
    request_digest: Digest
    plan_id: PlanId
    plan_digest: Digest
    recording_id: RecordingId
    first_completed_utc_ns: UtcNs

    def identity_digest(self) -> Digest:
        return canonical_digest(
            {
                "request_digest": self.request_digest,
                "plan_id": self.plan_id,
                "plan_digest": self.plan_digest,
                "recording_id": self.recording_id,
            }
        )


@dataclass(frozen=True)
class SupervisorHealth:
    state: str
    ready: bool
    updated_utc_ns: UtcNs
    completed_units: int
    failed_units: int
    recovered_recordings: int
    abandoned_allocations: int
    startup_published: int
    startup_cleaned: int
    capacity_status: str | None
    clock_source: str | None
    last_receipt_digest: Digest | None
    detail: str | None = None


class SQLiteSupervisorState:
    """Durable operational health and stable plan-to-recording receipts."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS dwell_supervisor_receipts (
                    plan_id TEXT PRIMARY KEY,
                    request_digest TEXT NOT NULL,
                    plan_digest TEXT NOT NULL,
                    recording_id TEXT NOT NULL UNIQUE,
                    first_completed_utc_ns INTEGER NOT NULL,
                    identity_digest TEXT NOT NULL UNIQUE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS dwell_supervisor_health (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    payload TEXT NOT NULL
                )
                """
            )

    def record_receipt(
        self,
        request: DwellRequest,
        schedule: DwellCaptureReceipt,
        completed_utc_ns: UtcNs,
    ) -> DurableDwellReceipt:
        candidate = DurableDwellReceipt(
            canonical_digest(request),
            schedule.plan_id,
            schedule.plan_digest,
            schedule.recording_id,
            completed_utc_ns,
        )
        identity = candidate.identity_digest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM dwell_supervisor_receipts WHERE plan_id = ?",
                (str(schedule.plan_id),),
            ).fetchone()
            if row is not None:
                restored = _receipt(row)
                if restored.identity_digest() != identity:
                    raise SupervisorStateError(
                        "durable supervisor receipt identity conflict"
                    )
                return restored
            connection.execute(
                """
                INSERT INTO dwell_supervisor_receipts(
                    plan_id, request_digest, plan_digest, recording_id,
                    first_completed_utc_ns, identity_digest
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(candidate.plan_id),
                    str(candidate.request_digest),
                    str(candidate.plan_digest),
                    str(candidate.recording_id),
                    int(candidate.first_completed_utc_ns),
                    str(identity),
                ),
            )
        return candidate

    def record_health(self, health: SupervisorHealth) -> None:
        payload = json.dumps(
            {
                "abandoned_allocations": health.abandoned_allocations,
                "capacity_status": health.capacity_status,
                "clock_source": health.clock_source,
                "completed_units": health.completed_units,
                "detail": health.detail,
                "failed_units": health.failed_units,
                "last_receipt_digest": (
                    None
                    if health.last_receipt_digest is None
                    else str(health.last_receipt_digest)
                ),
                "ready": health.ready,
                "recovered_recordings": health.recovered_recordings,
                "startup_cleaned": health.startup_cleaned,
                "startup_published": health.startup_published,
                "state": health.state,
                "updated_utc_ns": int(health.updated_utc_ns),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO dwell_supervisor_health(singleton, payload) VALUES (1, ?)
                ON CONFLICT(singleton) DO UPDATE SET payload = excluded.payload
                """,
                (payload,),
            )

    def health(self) -> SupervisorHealth | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM dwell_supervisor_health WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["payload"])
        digest = value["last_receipt_digest"]
        return SupervisorHealth(
            value["state"],
            value["ready"],
            UtcNs(value["updated_utc_ns"]),
            value["completed_units"],
            value["failed_units"],
            value["recovered_recordings"],
            value["abandoned_allocations"],
            value["startup_published"],
            value["startup_cleaned"],
            value["capacity_status"],
            value["clock_source"],
            _digest(digest) if digest is not None else None,
            value["detail"],
        )


class SupervisorPreflightError(RuntimeError):
    pass


class SupervisorStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class SupervisorProcessResult:
    schedule: DwellCaptureReceipt
    durable_receipt: DurableDwellReceipt


class V5DwellSupervisor:
    """Hold exclusive capture ownership and supervise one bounded unit at a time."""

    def __init__(
        self,
        *,
        host_guard: _HostGuard,
        clock: TrustedCaptureClock,
        spool: SQLiteLocalSpool,
        local_recordings: RootedSigMFRecordingStore,
        reconciler: PublicationReconciler,
        scheduler: OneShotDwellCaptureScheduler,
        capacity: CapacityConfiguration,
        retention: RetentionCapacityPolicy,
        state: SQLiteSupervisorState,
    ) -> None:
        self._host_guard = host_guard
        self._clock = clock
        self._spool = spool
        self._local = local_recordings
        self._reconciler = reconciler
        self._scheduler = scheduler
        self._capacity = capacity
        self._retention = retention
        self._state_store = state
        self._health: SupervisorHealth | None = None
        self._started = False
        self._closed = False

    def start(self) -> None:
        if self._started:
            return
        if self._closed:
            raise SupervisorStateError("closed dwell supervisor cannot restart")
        self._host_guard.acquire()
        try:
            now = UtcNs(self._clock.now_utc_ns())
            recovered, abandoned = self._recover()
            reconciliation = self._reconciler.reconcile()
            _require_reconciled(reconciliation)
            capacity = self._capacity_report()
            self._require_retention(0, allow_full=True)
            proof = self._clock.last_attestation
            assert proof is not None
            self._health = SupervisorHealth(
                "ready",
                True,
                now,
                0,
                0,
                recovered,
                abandoned,
                reconciliation.published,
                reconciliation.cleaned,
                str(capacity["overall_status"]),
                proof.source,
                None,
            )
            self._state_store.record_health(self._health)
            self._started = True
        except Exception as error:
            self._host_guard.close()
            self._record_failed_health(error)
            raise

    def process(self, request: DwellRequest) -> SupervisorProcessResult:
        if (
            not self._started
            or self._closed
            or self._health is None
            or not self._health.ready
        ):
            raise SupervisorStateError("dwell supervisor is not ready")
        try:
            now = UtcNs(self._clock.now_utc_ns())
            planned_bytes = (
                request.sample_count * self._retention.bytes_per_paired_sample
                + self._retention.metadata_reserve_bytes_per_recording
            )
            self._capacity_report(required_peak_bytes=planned_bytes * 2)
            self._require_retention(planned_bytes, allow_full=False)
            schedule = self._scheduler.run(request, now)
            completed = UtcNs(self._clock.now_utc_ns())
            durable = self._state_store.record_receipt(request, schedule, completed)
            self._health = SupervisorHealth(
                "ready",
                True,
                completed,
                self._health.completed_units + 1,
                self._health.failed_units,
                self._health.recovered_recordings,
                self._health.abandoned_allocations,
                self._health.startup_published,
                self._health.startup_cleaned,
                self._health.capacity_status,
                self._health.clock_source,
                durable.identity_digest(),
            )
            self._state_store.record_health(self._health)
            return SupervisorProcessResult(schedule, durable)
        except Exception as error:
            self._record_failed_health(error)
            raise

    def health(self) -> SupervisorHealth:
        if self._health is None:
            persisted = self._state_store.health()
            if persisted is None:
                raise SupervisorStateError("supervisor health is unavailable")
            return persisted
        return self._health

    def close(self, timeout_s: float) -> None:
        if timeout_s <= 0:
            raise ValueError("shutdown timeout must be positive")
        if self._closed:
            return
        self._closed = True
        self._host_guard.close()
        if self._health is not None:
            self._health = SupervisorHealth(
                "stopped",
                False,
                self._health.updated_utc_ns,
                self._health.completed_units,
                self._health.failed_units,
                self._health.recovered_recordings,
                self._health.abandoned_allocations,
                self._health.startup_published,
                self._health.startup_cleaned,
                self._health.capacity_status,
                self._health.clock_source,
                self._health.last_receipt_digest,
            )
            self._state_store.record_health(self._health)

    def _recover(self) -> tuple[int, int]:
        recovered_count = 0
        abandoned_count = 0
        for entry in self._spool.incomplete_allocations():
            try:
                recovered = self._local.recover_finalized(
                    entry.recording_id, entry.plan_id, entry.destination
                )
            except LocalRecordingNotFinalizedError:
                self._local.quarantine_incomplete(entry.recording_id, entry.destination)
                self._spool.record_failure(
                    entry.recording_id, "capture process restarted"
                )
                abandoned_count += 1
            else:
                self._spool.record_complete(recovered)
                recovered_count += 1
        return recovered_count, abandoned_count

    def _capacity_report(self, required_peak_bytes: int = 0) -> dict[str, object]:
        report = check_capacity(self._capacity, now_utc_ns=self._clock.now_utc_ns)
        if exit_code(report, self._capacity.fail_on):
            raise SupervisorPreflightError("storage capacity status rejected capture")
        roots = report["roots"]
        assert isinstance(roots, list)
        free = [
            item["filesystem_free_bytes"]
            for item in roots
            if "filesystem_free_bytes" in item
        ]
        if not free or min(free) < required_peak_bytes:
            raise SupervisorPreflightError("planned capture peak exceeds free space")
        return report

    def _require_retention(self, planned_bytes: int, *, allow_full: bool) -> None:
        entries = self._spool.durable_recordings(
            self._retention.maximum_retained_recordings + 1
        )
        retained_bytes = sum(
            entry.recording.data_object.byte_count
            + entry.recording.metadata_object.byte_count
            for entry in entries
            if entry.recording is not None
        )
        count_limit = self._retention.maximum_retained_recordings
        if (
            len(entries) > count_limit
            or retained_bytes > self._retention.maximum_retained_bytes
        ):
            raise SupervisorPreflightError(
                "retained capture budget is already exceeded"
            )
        if not allow_full and (
            len(entries) >= count_limit
            or retained_bytes + planned_bytes > self._retention.maximum_retained_bytes
        ):
            raise SupervisorPreflightError("request exceeds retained capture budget")

    def _record_failed_health(self, error: Exception) -> None:
        prior = self._health
        proof = self._clock.last_attestation
        now = (
            prior.updated_utc_ns
            if prior is not None
            else UtcNs(0)
            if proof is None
            else proof.observed_utc_ns
        )
        self._health = SupervisorHealth(
            "failed",
            False,
            now,
            0 if prior is None else prior.completed_units,
            1 if prior is None else prior.failed_units + 1,
            0 if prior is None else prior.recovered_recordings,
            0 if prior is None else prior.abandoned_allocations,
            0 if prior is None else prior.startup_published,
            0 if prior is None else prior.startup_cleaned,
            None if prior is None else prior.capacity_status,
            None if prior is None else prior.clock_source,
            None if prior is None else prior.last_receipt_digest,
            f"{type(error).__name__}: operation failed",
        )
        self._state_store.record_health(self._health)


def one_request_service(
    supervisor: V5DwellSupervisor,
    request: DwellRequest,
    *,
    instance_id: str,
    shutdown_timeout_s: float = 10.0,
) -> ServiceLoop:
    """Use the shared signal-aware lifecycle for one injected request."""

    pending = True

    def step() -> bool:
        nonlocal pending
        if not pending:
            return False
        supervisor.process(request)
        pending = False
        return True

    return ServiceLoop(
        service="v5-dwell-capture",
        instance_id=instance_id,
        start=supervisor.start,
        step=step,
        close=supervisor.close,
        shutdown_timeout_s=shutdown_timeout_s,
    )


def _require_reconciled(result: ReconciliationResult) -> None:
    if result.deferred:
        raise SupervisorPreflightError("startup spool reconciliation remains deferred")


def _digest(value: str) -> Digest:
    algorithm, digest = value.split(":", 1)
    return Digest(DigestAlgorithm(algorithm), digest)


def _receipt(row: sqlite3.Row) -> DurableDwellReceipt:
    return DurableDwellReceipt(
        _digest(row["request_digest"]),
        PlanId(row["plan_id"]),
        _digest(row["plan_digest"]),
        RecordingId(row["recording_id"]),
        UtcNs(row["first_completed_utc_ns"]),
    )
