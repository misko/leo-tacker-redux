"""Integration-owned runtime adapters for the finite Gauss V5 campaign."""

from __future__ import annotations

import math
import multiprocessing
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.connection import wait as wait_for_connections
from pathlib import Path
from typing import Any, Protocol, cast

from leo_flow.adapters.campaign_analysis_receipt_postgres import (
    FeatureProjectionReceiptEvidence,
    PostgresCampaignProjectionReceiptReader,
)
from leo_flow.adapters.capture_batch_sqlite import SQLiteCaptureBatchStateStore
from leo_flow.adapters.dashboard_batch_postgres import (
    PostgresCaptureBatchDashboardRepository,
    PostgresCaptureBatchProjectionWriter,
)
from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.adapters.waterfall_receipt_postgres import (
    PostgresWaterfallReceiptReaderV0_1,
    WaterfallAnalysisReceiptV0_1,
)
from leo_flow.application.capture_admission import CaptureAdmissionGate
from leo_flow.application.capture_batch_dashboard import (
    CaptureBatchDashboardPublisher,
)
from leo_flow.application.capture_batches import CaptureBatchCoordinator
from leo_flow.capture.campaign import (
    CampaignAnalysisPort,
    CampaignAnalysisReceipt,
    CampaignAnalysisSuccess,
    CampaignCapacityPort,
    CampaignCapturePort,
    CampaignDefinition,
    CampaignUnit,
    materialize_campaign_station,
)
from leo_flow.capture.dual import DualCaptureExecutor, UtcCoordinatedReleaseGate
from leo_flow.capture.v5_station import (
    V5CaptureStation,
    require_disjoint_station_pair,
)
from leo_flow.contracts.capture_batch import CaptureBatchSnapshot
from leo_flow.contracts.core import JobId, RecordingId, UtcNs
from leo_flow.contracts.dashboard_batch import (
    DashboardAnalysisState,
    DashboardCaptureState,
)
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.deployments.process_isolated_capture import (
    SpawnProcessSupervisor,
    _arm_parent_death_signal,
    _scrub_child_environment,
    _silence_child_standard_streams,
)
from leo_flow.deployments.process_mode_lock import ExclusiveModeLock
from leo_flow.deployments.recording_submission_v1 import analysis_connection_factory
from leo_flow.deployments.v5_dual_capture_operator import (
    _build_process_isolated_runner,
    _postgres_drain_gate,
    _projection_writer,
    _validate_pair,
)
from leo_flow.jobs.contracts import JobSnapshot, JobState
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from leo_flow.services.capture_batch_analysis import (
    ClosedBatchAnalysisSubmissionService,
    SubmittedClosedBatchAnalysis,
)
from leo_flow.services.config import AnalysisServiceConfig
from leo_flow.services.waterfall_submission import (
    WaterfallAnalysisSubmissionServiceV0_1,
    WaterfallAnalysisSubmissionV0_1,
)
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
from leo_station.analysis_operator import (
    _batch_selection,
    _process_one,
    _process_starlink_suite_one,
    _process_waterfall_one,
    _project_one,
    _project_starlink_suite_one,
    _project_waterfall_one,
    _submit_starlink_suite,
)
from leo_station.analysis_v1 import (
    MODE_LOCK_PATH,
    WATERFALL_ALGORITHM_REF,
    WATERFALL_CONFIG_REF,
    WATERFALL_DEPENDENCY_REFS,
)


class CampaignJobReader(Protocol):
    def snapshot(self, job_id: JobId) -> JobSnapshot: ...


class CampaignProjectionReceiptReader(Protocol):
    def read(self, source_job_id: JobId) -> FeatureProjectionReceiptEvidence | None: ...


class CampaignWaterfallReceiptReader(Protocol):
    def read(self, source_job_id: JobId) -> WaterfallAnalysisReceiptV0_1 | None: ...


@dataclass(frozen=True)
class StarlinkSuiteReceiptEvidenceV0_2:
    work_id: str
    source_job_id: JobId
    work_state: str
    recording_id: RecordingId
    result_state: str
    suite_count: int
    method_count: int
    projected_utc_ns: UtcNs | None


class CampaignStarlinkSuiteReceiptReader(Protocol):
    def read(self, source_job_id: JobId) -> StarlinkSuiteReceiptEvidenceV0_2 | None: ...


class PostgresStarlinkSuiteReceiptReaderV0_2:
    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect

    def read(self, source_job_id: JobId) -> StarlinkSuiteReceiptEvidenceV0_2 | None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM public.read_starlink_detector_suite_receipt(%s)",
                (str(source_job_id),),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise CampaignAnalysisError("detector-suite receipt is ambiguous")
        row = rows[0]
        projected = row["projected_at_utc"]
        return StarlinkSuiteReceiptEvidenceV0_2(
            str(row["work_id"]),
            JobId(str(row["source_job_id"])),
            str(row["work_state"]),
            RecordingId(str(row["recording_id"])),
            str(row["result_state"]),
            int(row["suite_count"]),
            int(row["method_count"]),
            None
            if projected is None
            else UtcNs(round(projected.timestamp() * 1_000_000_000)),
        )


class CampaignAnalysisError(RuntimeError):
    """Exact campaign analysis or projection evidence did not converge."""


class ExternalRadioOwnershipGate(Protocol):
    def require_clear(self) -> None: ...


class CampaignCaptureTimingDefinition(Protocol):
    @property
    def maximum_start_lateness_ns(self) -> int: ...


class CampaignCaptureError(RuntimeError):
    """A live dual campaign capture cannot safely enter or close."""


class LinuxExternalRadioOwnershipGate:
    """Observational fail-closed gate for foreign owners of exact radio IPs."""

    def __init__(
        self,
        radio_ips: tuple[str, str],
        *,
        proc_root: Path = Path("/proc"),
    ) -> None:
        if len(set(radio_ips)) != 2:
            raise ValueError("campaign ownership gate requires two distinct radio IPs")
        for value in radio_ips:
            socket.inet_aton(value)
        self._radio_ips = radio_ips
        self._proc_root = proc_root

    def require_clear(self) -> None:
        if self._cmdline_owner_exists() or self._tcp_owner_exists():
            raise CampaignCaptureError("an external process owns a campaign radio")

    def _cmdline_owner_exists(self) -> bool:
        try:
            entries = tuple(self._proc_root.iterdir())
        except OSError as error:
            raise CampaignCaptureError(
                "process ownership evidence is unavailable"
            ) from error
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                payload = (entry / "cmdline").read_bytes()
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            except OSError as error:
                raise CampaignCaptureError(
                    "process ownership evidence cannot be read"
                ) from error
            if any(value.encode("ascii") in payload for value in self._radio_ips):
                return True
        return False

    def _tcp_owner_exists(self) -> bool:
        path = self._proc_root / "net" / "tcp"
        try:
            rows = path.read_text(encoding="ascii").splitlines()[1:]
        except OSError as error:
            raise CampaignCaptureError(
                "TCP ownership evidence is unavailable"
            ) from error
        for row in rows:
            fields = row.split()
            if len(fields) < 4 or fields[3] in {"06", "07"}:
                continue
            remote = fields[2].partition(":")[0]
            try:
                address = socket.inet_ntoa(bytes.fromhex(remote)[::-1])
            except (OSError, ValueError):
                raise CampaignCaptureError("TCP ownership evidence is malformed")
            if address in self._radio_ips:
                return True
        return False


class ProcessIsolatedCampaignCapture(CampaignCapturePort):
    """Compose one exact campaign unit from the qualified dual-radio runner."""

    def __init__(
        self,
        definition: CampaignCaptureTimingDefinition,
        station_a: V5CaptureStation,
        station_b: V5CaptureStation,
        campaign_state_root: Path,
        batch_database: Path,
        credential_directory: Path,
        ownership: ExternalRadioOwnershipGate,
        *,
        station_materializer: Callable[[CampaignUnit, str], V5CaptureStation]
        | None = None,
        admission_builder: Callable[[str], CaptureAdmissionGate] | None = None,
        secondary_dispatch_delay_s: float = 0.0,
        now_utc_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if (
            isinstance(secondary_dispatch_delay_s, bool)
            or not isinstance(secondary_dispatch_delay_s, (int, float))
            or not math.isfinite(secondary_dispatch_delay_s)
            or not 0 <= secondary_dispatch_delay_s < 0.1
        ):
            raise ValueError(
                "campaign secondary dispatch delay must be within the skew bound"
            )
        self._definition = definition
        self._station_a = station_a
        self._station_b = station_b
        self._state_root = campaign_state_root
        self._batch_database = batch_database
        self._credential_directory = credential_directory
        self._ownership = ownership
        self._station_materializer = station_materializer or (
            lambda unit, side: materialize_campaign_station(
                cast(CampaignDefinition, self._definition),
                self._station_a if side == "a" else self._station_b,
                unit,
                side=side,
                campaign_state_root=self._state_root,
            )
        )
        self._admission_builder = admission_builder or _postgres_drain_gate
        self._secondary_dispatch_delay_s = secondary_dispatch_delay_s
        self._now = now_utc_ns

    def capture(
        self,
        unit: CampaignUnit,
        *,
        not_before_utc_ns: UtcNs,
        deadline_utc_ns: UtcNs,
    ) -> CaptureBatchSnapshot:
        if not_before_utc_ns != unit.requested_start_utc_ns or int(
            deadline_utc_ns
        ) <= int(not_before_utc_ns):
            raise CampaignCaptureError("campaign capture timing identity differs")
        first = self._station_materializer(unit, "a")
        second = self._station_materializer(unit, "b")
        require_disjoint_station_pair(first, second)
        ordered = _validate_pair(unit.batch, first, second)
        self._ownership.require_clear()
        lock = ExclusiveModeLock(first.state.mode_lock_path)
        supervisor: SpawnProcessSupervisor | None = None
        lock.acquire()
        try:
            self._ownership.require_clear()
            credential = SystemdCredentialProvider(self._credential_directory).resolve(
                "catalog-dsn"
            )
            if not self._admission_builder(credential).ready():
                raise CampaignCaptureError("capture admission gate is closed")
            store = SQLiteCaptureBatchStateStore(self._batch_database)
            coordinator = CaptureBatchCoordinator(store)
            existing = store.get(unit.batch.batch_id)
            if existing is not None:
                if not existing.terminal:
                    raise CampaignCaptureError(
                        "partial campaign batch cannot be recaptured"
                    )
                state = existing
            else:
                supervisor = SpawnProcessSupervisor()
                runners = {
                    station.radio.radio_id: _build_process_isolated_runner(
                        station,
                        credential,
                        unit.batch.batch_id,
                        supervisor,
                        post_release_dispatch_delay_s=(
                            0.0 if index == 0 else self._secondary_dispatch_delay_s
                        ),
                    )
                    for index, station in enumerate(ordered)
                }
                remaining_s = (int(deadline_utc_ns) - self._now()) / 1_000_000_000
                if remaining_s <= 0:
                    raise CampaignCaptureError("campaign capture slot already expired")
                state = DualCaptureExecutor(
                    coordinator,
                    startup_timeout_s=min(30.0, remaining_s),
                    finish_timeout_s=min(900.0, remaining_s),
                    cleanup_timeout_s=min(15.0, remaining_s),
                    now_utc_ns=self._now,
                    coordinated_release_admission=UtcCoordinatedReleaseGate(
                        self._definition.maximum_start_lateness_ns,
                        now_utc_ns=self._now,
                    ),
                ).execute(unit.batch, runners, deadline_utc_ns=deadline_utc_ns)
                supervisor.abort_all()
            CaptureBatchDashboardPublisher(
                _projection_writer(credential)
            ).publish_initial(state)
            self._ownership.require_clear()
            return state
        finally:
            if supervisor is not None:
                try:
                    supervisor.abort_all()
                finally:
                    lock.release()
            else:
                lock.release()


class LocalCampaignCapacity(CampaignCapacityPort):
    """Report the minimum available bytes across durable CAS and local staging."""

    def __init__(self, cas_root: Path, staging_root: Path) -> None:
        for path in (cas_root, staging_root):
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("campaign capacity paths must be absolute")
        self._cas_root = cas_root
        self._staging_root = staging_root

    def available_bytes(self) -> int:
        return min(
            _available_bytes(self._cas_root), _available_bytes(self._staging_root)
        )


class ExactCampaignAnalysis(CampaignAnalysisPort):
    """Drain and prove exact FeatureSet and optional waterfall projections."""

    def __init__(
        self,
        submit: Callable[[CaptureBatchSnapshot], SubmittedClosedBatchAnalysis],
        jobs: CampaignJobReader,
        projections: CampaignProjectionReceiptReader,
        dashboard: PostgresCaptureBatchDashboardRepository,
        process_one: Callable[[UtcNs], bool],
        project_one: Callable[[UtcNs], bool],
        *,
        submit_waterfalls: Callable[[CaptureBatchSnapshot], dict[RecordingId, JobId]]
        | None = None,
        waterfall_receipts: CampaignWaterfallReceiptReader | None = None,
        process_waterfall_one: Callable[[UtcNs], bool] | None = None,
        project_waterfall_one: Callable[[UtcNs], bool] | None = None,
        submit_starlink_suites: Callable[
            [CaptureBatchSnapshot], dict[RecordingId, JobId]
        ]
        | None = None,
        starlink_suite_receipts: CampaignStarlinkSuiteReceiptReader | None = None,
        process_starlink_suites: Callable[[int, UtcNs], bool] | None = None,
        project_starlink_suite_one: Callable[[UtcNs], bool] | None = None,
        now_utc_ns: Callable[[], int] = time.time_ns,
        delay: Callable[[float], None] = time.sleep,
        poll_interval_s: float = 0.05,
    ) -> None:
        if poll_interval_s <= 0:
            raise ValueError("campaign analysis poll interval must be positive")
        waterfall_parts = (
            submit_waterfalls,
            waterfall_receipts,
            process_waterfall_one,
            project_waterfall_one,
        )
        if any(item is not None for item in waterfall_parts) and not all(
            item is not None for item in waterfall_parts
        ):
            raise ValueError("campaign waterfall composition must be complete")
        suite_parts = (
            submit_starlink_suites,
            starlink_suite_receipts,
            process_starlink_suites,
            project_starlink_suite_one,
        )
        if any(item is not None for item in suite_parts) and not all(
            item is not None for item in suite_parts
        ):
            raise ValueError("campaign detector-suite composition must be complete")
        self._submit = submit
        self._jobs = jobs
        self._projections = projections
        self._dashboard = dashboard
        self._process_one = process_one
        self._project_one = project_one
        self._submit_waterfalls = submit_waterfalls
        self._waterfall_receipts = waterfall_receipts
        self._process_waterfall_one = process_waterfall_one
        self._project_waterfall_one = project_waterfall_one
        self._submit_starlink_suites = submit_starlink_suites
        self._starlink_suite_receipts = starlink_suite_receipts
        self._process_starlink_suites = process_starlink_suites
        self._project_starlink_suite_one = project_starlink_suite_one
        self._now = now_utc_ns
        self._delay = delay
        self._poll_interval_s = poll_interval_s

    def analyze(
        self, snapshot: CaptureBatchSnapshot, *, deadline_utc_ns: UtcNs
    ) -> CampaignAnalysisReceipt:
        submitted = self._submit(snapshot)
        expected_recordings = tuple(
            sorted(
                snapshot.successful_recordings, key=lambda item: str(item.recording_id)
            )
        )
        jobs_by_recording = {
            item.request.recording_id: item.job_id for item in submitted.recording_jobs
        }
        if (
            len(expected_recordings) != 2
            or len(jobs_by_recording) != 2
            or set(jobs_by_recording)
            != {item.recording_id for item in expected_recordings}
        ):
            raise CampaignAnalysisError("closed batch did not submit two exact jobs")
        waterfall_jobs = (
            {} if self._submit_waterfalls is None else self._submit_waterfalls(snapshot)
        )
        if self._submit_waterfalls is not None and (
            len(waterfall_jobs) != 2
            or set(waterfall_jobs)
            != {item.recording_id for item in expected_recordings}
        ):
            raise CampaignAnalysisError(
                "closed batch did not submit two exact waterfall jobs"
            )
        starlink_suite_jobs = (
            {}
            if self._submit_starlink_suites is None
            else self._submit_starlink_suites(snapshot)
        )
        if self._submit_starlink_suites is not None and (
            len(starlink_suite_jobs) != 2
            or set(starlink_suite_jobs)
            != {item.recording_id for item in expected_recordings}
        ):
            raise CampaignAnalysisError(
                "closed batch did not submit two exact detector-suite jobs"
            )

        while self._now() < int(deadline_utc_ns):
            job_snapshots = {
                recording.recording_id: self._jobs.snapshot(
                    jobs_by_recording[recording.recording_id]
                )
                for recording in expected_recordings
            }
            _reject_terminal_job_failures(job_snapshots)
            feature_jobs_complete = all(
                item.state is JobState.SUCCEEDED and item.result_ref is not None
                for item in job_snapshots.values()
            )
            if feature_jobs_complete:
                receipts = {
                    recording.recording_id: self._projections.read(
                        jobs_by_recording[recording.recording_id]
                    )
                    for recording in expected_recordings
                }
                _reject_terminal_projection_failures(receipts)
                feature_projection_complete = all(
                    item is not None
                    and item.state == "succeeded"
                    and item.projected_utc_ns is not None
                    for item in receipts.values()
                )
                if feature_projection_complete and waterfall_jobs:
                    assert self._waterfall_receipts is not None
                    waterfall_snapshots = {
                        recording_id: self._jobs.snapshot(job_id)
                        for recording_id, job_id in waterfall_jobs.items()
                    }
                    _reject_terminal_job_failures(waterfall_snapshots)
                    if not all(
                        item.state is JobState.SUCCEEDED and item.result_ref is not None
                        for item in waterfall_snapshots.values()
                    ):
                        assert self._process_waterfall_one is not None
                        progressed = self._process_waterfall_one(deadline_utc_ns)
                    else:
                        waterfall_receipts = {
                            recording_id: self._waterfall_receipts.read(job_id)
                            for recording_id, job_id in waterfall_jobs.items()
                        }
                        _reject_terminal_waterfall_failures(waterfall_receipts)
                        if all(
                            item is not None
                            and item.work_state == "succeeded"
                            and item.projected_utc_ns is not None
                            for item in waterfall_receipts.values()
                        ):
                            suite_complete, progressed = self._advance_starlink_suites(
                                expected_recordings,
                                starlink_suite_jobs,
                                deadline_utc_ns,
                            )
                            if suite_complete:
                                return self._complete(
                                    snapshot,
                                    expected_recordings,
                                    jobs_by_recording,
                                    job_snapshots,
                                    receipts,
                                    deadline_utc_ns,
                                )
                        assert self._project_waterfall_one is not None
                        if not all(
                            item is not None
                            and item.work_state == "succeeded"
                            and item.projected_utc_ns is not None
                            for item in waterfall_receipts.values()
                        ):
                            progressed = self._project_waterfall_one(deadline_utc_ns)
                elif feature_projection_complete:
                    suite_complete, progressed = self._advance_starlink_suites(
                        expected_recordings, starlink_suite_jobs, deadline_utc_ns
                    )
                    if suite_complete:
                        return self._complete(
                            snapshot,
                            expected_recordings,
                            jobs_by_recording,
                            job_snapshots,
                            receipts,
                            deadline_utc_ns,
                        )
                else:
                    progressed = self._project_one(deadline_utc_ns)
            else:
                progressed = self._process_one(deadline_utc_ns)
            if not progressed:
                remaining_s = max(
                    0.0, (int(deadline_utc_ns) - self._now()) / 1_000_000_000
                )
                self._delay(min(self._poll_interval_s, remaining_s))
        raise CampaignAnalysisError("campaign analysis exceeded its absolute deadline")

    def _advance_starlink_suites(
        self,
        recordings: tuple[PublishedRecordingRef, ...],
        jobs: dict[RecordingId, JobId],
        deadline_utc_ns: UtcNs,
    ) -> tuple[bool, bool]:
        if not jobs:
            return True, False
        snapshots = {
            recording_id: self._jobs.snapshot(job_id)
            for recording_id, job_id in jobs.items()
        }
        _reject_terminal_job_failures(snapshots)
        if not all(
            item.state is JobState.SUCCEEDED and item.result_ref is not None
            for item in snapshots.values()
        ):
            unfinished = sum(
                item.state is not JobState.SUCCEEDED or item.result_ref is None
                for item in snapshots.values()
            )
            assert self._process_starlink_suites is not None
            return False, self._process_starlink_suites(unfinished, deadline_utc_ns)
        assert self._starlink_suite_receipts is not None
        receipts = {
            recording_id: self._starlink_suite_receipts.read(job_id)
            for recording_id, job_id in jobs.items()
        }
        if any(
            item is not None and item.work_state == "parked"
            for item in receipts.values()
        ):
            raise CampaignAnalysisError("exact detector-suite projection was parked")
        for recording in recordings:
            receipt = receipts[recording.recording_id]
            if (
                receipt is None
                or receipt.recording_id != recording.recording_id
                or receipt.source_job_id != jobs[recording.recording_id]
            ):
                raise CampaignAnalysisError(
                    "exact detector-suite receipt identity differs"
                )
            if receipt.result_state == "candidates":
                if (
                    receipt.suite_count <= 0
                    or receipt.method_count != receipt.suite_count * 8
                ):
                    raise CampaignAnalysisError("detector-suite method closure differs")
            elif receipt.result_state == "not_evaluated":
                if receipt.suite_count != 0 or receipt.method_count != 0:
                    raise CampaignAnalysisError(
                        "not-evaluated detector-suite receipt differs"
                    )
            else:
                raise CampaignAnalysisError("unknown detector-suite terminal state")
        if all(
            item is not None
            and item.work_state == "succeeded"
            and item.projected_utc_ns is not None
            for item in receipts.values()
        ):
            return True, False
        assert self._project_starlink_suite_one is not None
        return False, self._project_starlink_suite_one(deadline_utc_ns)

    def _complete(
        self,
        snapshot: CaptureBatchSnapshot,
        recordings: tuple[PublishedRecordingRef, ...],
        jobs_by_recording: dict[RecordingId, JobId],
        job_snapshots: dict[RecordingId, JobSnapshot],
        receipts: dict[RecordingId, FeatureProjectionReceiptEvidence | None],
        deadline_utc_ns: UtcNs,
    ) -> CampaignAnalysisReceipt:
        dashboard = self._dashboard.capture_batch(snapshot.batch_id)
        dashboard_attempts = {item.recording_id: item for item in dashboard.attempts}
        expected_ids = {item.recording_id for item in recordings}
        if set(dashboard_attempts) != expected_ids or any(
            item.capture_state is not DashboardCaptureState.SUCCEEDED
            or item.analysis_state is not DashboardAnalysisState.COMPLETE
            or not item.analysis_result_available
            for item in dashboard_attempts.values()
        ):
            raise CampaignAnalysisError(
                "dashboard did not converge for the exact batch"
            )
        successes: list[CampaignAnalysisSuccess] = []
        for recording_id in sorted(expected_ids, key=str):
            job = job_snapshots[recording_id]
            evidence = receipts[recording_id]
            if (
                job.result_ref is None
                or evidence is None
                or evidence.projected_utc_ns is None
            ):
                raise CampaignAnalysisError("exact analysis receipt is incomplete")
            if (
                evidence.recording_id != recording_id
                or evidence.source_job_id != jobs_by_recording[recording_id]
                or evidence.job_result != job.result_ref
            ):
                raise CampaignAnalysisError("exact analysis receipt identity differs")
            successes.append(
                CampaignAnalysisSuccess(
                    recording_id,
                    evidence.source_job_id,
                    job.result_ref,
                    evidence.work_id,
                    evidence.feature_ref,
                    evidence.projected_utc_ns,
                )
            )
        completed = UtcNs(self._now())
        if completed > deadline_utc_ns:
            raise CampaignAnalysisError(
                "campaign analysis completed after its deadline"
            )
        return CampaignAnalysisReceipt(
            snapshot.batch_id, (successes[0], successes[1]), completed
        )


class LockedCampaignAnalysis(CampaignAnalysisPort):
    """Hold the shared capture/analysis exclusion lock for the analysis stage."""

    def __init__(self, delegate: CampaignAnalysisPort) -> None:
        self._delegate = delegate

    def analyze(
        self, snapshot: CaptureBatchSnapshot, *, deadline_utc_ns: UtcNs
    ) -> CampaignAnalysisReceipt:
        lock = ExclusiveModeLock(MODE_LOCK_PATH)
        lock.acquire()
        try:
            return self._delegate.analyze(snapshot, deadline_utc_ns=deadline_utc_ns)
        finally:
            lock.release()


def build_gauss_campaign_analysis(
    config: AnalysisServiceConfig,
    analysis_credential_directory: Path,
    dashboard_credential_directory: Path,
    *,
    lock_analysis: bool = True,
) -> CampaignAnalysisPort:
    analysis_credentials = SystemdCredentialProvider(analysis_credential_directory)
    analysis_connect = analysis_connection_factory(
        analysis_credentials.resolve("catalog-dsn")
    )
    dashboard_connect = _dashboard_connection_factory(
        SystemdCredentialProvider(dashboard_credential_directory).resolve("catalog-dsn")
    )
    jobs = PostgresJobLeaseRepository(analysis_connect)
    recordings = PostgresRecordingCatalog(analysis_connect)
    batch_projection = PostgresCaptureBatchProjectionWriter(analysis_connect)
    submission = ClosedBatchAnalysisSubmissionService(recordings, jobs)
    waterfall_submission = WaterfallAnalysisSubmissionServiceV0_1(jobs)

    def submit(snapshot: CaptureBatchSnapshot) -> SubmittedClosedBatchAnalysis:
        CaptureBatchDashboardPublisher(batch_projection).publish_initial(snapshot)
        return submission.submit(snapshot, _batch_selection())

    def submit_waterfalls(snapshot: CaptureBatchSnapshot) -> dict[RecordingId, JobId]:
        return {
            recording.recording_id: waterfall_submission.submit(
                WaterfallAnalysisSubmissionV0_1(
                    recording,
                    WATERFALL_ALGORITHM_REF,
                    WATERFALL_CONFIG_REF,
                    WATERFALL_DEPENDENCY_REFS,
                )
            ).job_id
            for recording in sorted(
                snapshot.successful_recordings,
                key=lambda item: str(item.recording_id),
            )
        }

    def submit_starlink_suites(
        snapshot: CaptureBatchSnapshot,
    ) -> dict[RecordingId, JobId]:
        return {
            recording.recording_id: _submit_starlink_suite(
                recording.recording_id, analysis_credentials
            ).job_id
            for recording in sorted(
                snapshot.successful_recordings, key=lambda item: str(item.recording_id)
            )
        }

    analysis: CampaignAnalysisPort = ExactCampaignAnalysis(
        submit,
        jobs,
        PostgresCampaignProjectionReceiptReader(analysis_connect),
        PostgresCaptureBatchDashboardRepository(dashboard_connect),
        lambda deadline: _run_analysis_child(
            "process", config, analysis_credential_directory, deadline
        ),
        lambda deadline: _run_analysis_child(
            "project", config, analysis_credential_directory, deadline
        ),
        submit_waterfalls=submit_waterfalls,
        waterfall_receipts=PostgresWaterfallReceiptReaderV0_1(analysis_connect),
        process_waterfall_one=lambda deadline: _run_analysis_child(
            "process-waterfall", config, analysis_credential_directory, deadline
        ),
        project_waterfall_one=lambda deadline: _run_analysis_child(
            "project-waterfall", config, analysis_credential_directory, deadline
        ),
        submit_starlink_suites=submit_starlink_suites,
        starlink_suite_receipts=PostgresStarlinkSuiteReceiptReaderV0_2(
            analysis_connect
        ),
        process_starlink_suites=lambda count, deadline: _run_analysis_children(
            "process-starlink-suite",
            config,
            analysis_credential_directory,
            deadline,
            child_count=count,
        ),
        project_starlink_suite_one=lambda deadline: _run_analysis_child(
            "project-starlink-suite",
            config,
            analysis_credential_directory,
            deadline,
        ),
    )
    return LockedCampaignAnalysis(analysis) if lock_analysis else analysis


def _run_analysis_child(
    operation: str,
    config: AnalysisServiceConfig,
    credential_directory: Path,
    deadline_utc_ns: UtcNs,
) -> bool:
    return _run_analysis_children(
        operation,
        config,
        credential_directory,
        deadline_utc_ns,
        child_count=1,
    )


@dataclass
class _SpawnedAnalysisChild:
    receiver: Connection
    sender: Connection
    process: Any
    started: bool = False


def _run_analysis_children(
    operation: str,
    config: AnalysisServiceConfig,
    credential_directory: Path,
    deadline_utc_ns: UtcNs,
    *,
    child_count: int,
    child_target: Callable[..., None] | None = None,
    child_target_args: tuple[Any, ...] | None = None,
) -> bool:
    """Run bounded isolated workers concurrently under one absolute deadline.

    Each worker crosses the existing database claim/lease boundary independently.
    A failed or late worker makes the group fail closed and all peers are reaped.
    The optional target arguments are an internal test seam; production always uses
    ``_analysis_child_main`` and its scrubbed, parent-death-armed process boundary.
    """

    if child_count < 1 or child_count > 2:
        raise CampaignAnalysisError("analysis child count must be within 1..2")
    remaining_s = (int(deadline_utc_ns) - time.time_ns()) / 1_000_000_000
    if remaining_s <= 0:
        raise CampaignAnalysisError("analysis child has no remaining deadline")
    context = multiprocessing.get_context("spawn")
    target = _analysis_child_main if child_target is None else child_target
    target_args = (
        (operation, config, credential_directory, os.getpid())
        if child_target_args is None
        else child_target_args
    )
    children: list[_SpawnedAnalysisChild] = []
    try:
        for index in range(child_count):
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(
                target=target,
                args=(sender, *target_args),
                name=f"gauss-campaign-{operation}-{index + 1}",
            )
            child = _SpawnedAnalysisChild(receiver, sender, process)
            children.append(child)
            process.start()
            child.started = True
            sender.close()

        pending = {child.receiver: child for child in children}
        progressed = False
        while pending:
            remaining_s = (int(deadline_utc_ns) - time.time_ns()) / 1_000_000_000
            if remaining_s <= 0:
                raise CampaignAnalysisError(
                    "analysis child exceeded its absolute deadline"
                )
            ready = wait_for_connections(tuple(pending), timeout=remaining_s)
            if not ready:
                raise CampaignAnalysisError(
                    "analysis child exceeded its absolute deadline"
                )
            for ready_item in ready:
                receiver = cast(Connection, ready_item)
                child = pending.pop(receiver)
                try:
                    outcome = receiver.recv()
                except (EOFError, OSError) as error:
                    raise CampaignAnalysisError("analysis child failed") from error
                remaining_s = max(
                    0.0,
                    (int(deadline_utc_ns) - time.time_ns()) / 1_000_000_000,
                )
                child.process.join(min(0.1, remaining_s))
                if child.process.is_alive() or child.process.exitcode != 0:
                    raise CampaignAnalysisError("analysis child failed")
                if outcome == ("ok", True):
                    progressed = True
                elif outcome != ("ok", False):
                    raise CampaignAnalysisError("analysis child failed")
        return progressed
    finally:
        _close_and_reap_analysis_children(children)


def _close_and_reap_analysis_children(
    children: list[_SpawnedAnalysisChild],
) -> None:
    for child in children:
        for connection in (child.receiver, child.sender):
            try:
                connection.close()
            except OSError:
                pass

    alive = [
        child
        for child in children
        if child.started and _analysis_process_is_alive(child.process)
    ]
    for child in alive:
        child.process.terminate()
    terminate_deadline = time.monotonic() + 0.25
    for child in alive:
        child.process.join(max(0.0, terminate_deadline - time.monotonic()))

    stubborn = [child for child in alive if _analysis_process_is_alive(child.process)]
    for child in stubborn:
        child.process.kill()
    kill_deadline = time.monotonic() + 0.25
    for child in stubborn:
        child.process.join(max(0.0, kill_deadline - time.monotonic()))

    for child in children:
        if child.started and _analysis_process_is_alive(child.process):
            continue
        try:
            child.process.close()
        except (OSError, ValueError):
            pass


def _analysis_process_is_alive(process: Any) -> bool:
    try:
        return bool(process.is_alive())
    except ValueError:
        return False


def _analysis_child_main(
    connection: Connection,
    operation: str,
    config: AnalysisServiceConfig,
    credential_directory: Path,
    expected_parent_pid: int,
) -> None:
    _arm_parent_death_signal(expected_parent_pid)
    _silence_child_standard_streams()
    _scrub_child_environment()
    try:
        if operation not in {
            "process",
            "project",
            "process-waterfall",
            "project-waterfall",
            "process-starlink-suite",
            "project-starlink-suite",
        }:
            raise CampaignAnalysisError("analysis child operation is invalid")
        credentials = SystemdCredentialProvider(credential_directory)
        with open(os.devnull, "w", encoding="utf-8") as output:
            if operation == "process":
                progressed = _process_one(config, credentials, output)
            elif operation == "project":
                progressed = _project_one(credentials)
            elif operation == "process-waterfall":
                progressed = _process_waterfall_one(credentials)
            elif operation == "process-starlink-suite":
                progressed = _process_starlink_suite_one(credentials)
            elif operation == "project-starlink-suite":
                progressed = _project_starlink_suite_one(credentials)
            else:
                progressed = _project_waterfall_one(credentials)
        connection.send(("ok", progressed))
    except Exception:  # noqa: BLE001 - sanitized parent boundary
        connection.send(("error",))
    finally:
        connection.close()


def _reject_terminal_job_failures(items: dict[RecordingId, JobSnapshot]) -> None:
    if any(item.state is JobState.PARKED for item in items.values()):
        raise CampaignAnalysisError("exact recording analysis was parked")


def _reject_terminal_projection_failures(
    items: dict[RecordingId, FeatureProjectionReceiptEvidence | None],
) -> None:
    if any(item is not None and item.state == "parked" for item in items.values()):
        raise CampaignAnalysisError("exact feature projection was parked")


def _reject_terminal_waterfall_failures(
    items: dict[RecordingId, WaterfallAnalysisReceiptV0_1 | None],
) -> None:
    if any(item is not None and item.work_state == "parked" for item in items.values()):
        raise CampaignAnalysisError("exact waterfall projection was parked")


def _available_bytes(path: Path) -> int:
    try:
        status = os.statvfs(path)
    except OSError as error:
        raise CampaignAnalysisError("campaign capacity path is unavailable") from error
    return status.f_bavail * status.f_frsize


def _dashboard_connection_factory(dsn: str) -> Callable[[], Any]:
    import psycopg
    from psycopg.rows import dict_row

    def connect() -> Any:
        connection = psycopg.connect(
            dsn,
            row_factory=dict_row,
            connect_timeout=5,
            options="-c statement_timeout=5000 -c lock_timeout=5000",
        )
        try:
            row = connection.execute(
                "SELECT pg_has_role(current_user, 'leo_dashboard', 'MEMBER') AS member"
            ).fetchone()
            if row is None or row["member"] is not True:
                raise CampaignAnalysisError(
                    "dashboard credential is not a leo_dashboard role member"
                )
            connection.execute("SET ROLE leo_dashboard")
            return connection
        except Exception:
            connection.close()
            raise

    return connect
