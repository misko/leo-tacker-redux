"""Exact bounded parallel analysis for one focused synchronized capture pair."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from leo_flow.adapters.campaign_scoped_claims_postgres import (
    PostgresCampaignAnalysisLaneStateReaderV1,
)
from leo_flow.adapters.dashboard_batch_postgres import (
    PostgresCaptureBatchProjectionWriter,
)
from leo_flow.adapters.focused_analysis_postgres import (
    PostgresFocusedAnalysisPairScopeRegistrarV0_1,
)
from leo_flow.adapters.hardware_link_postgres import (
    PostgresRecordingHardwareLinkCatalog,
)
from leo_flow.adapters.hardware_postgres_catalog import (
    PostgresHardwareSnapshotCatalog,
)
from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.application.capture_batch_dashboard import CaptureBatchDashboardPublisher
from leo_flow.capture.campaign import CampaignAnalysisReceipt
from leo_flow.contracts.capture_batch import CaptureBatchSnapshot
from leo_flow.contracts.core import Digest, JobId, RecordingId, SchemaRef, UtcNs
from leo_flow.contracts.deferred_analysis import (
    DeferredAnalysisLaneState,
    DeferredAnalysisStage,
    DeferredAnalysisWindowV1,
)
from leo_flow.contracts.features import FeatureSetBundle
from leo_flow.contracts.focused_analysis import FocusedAnalysisPairScopeV0_1
from leo_flow.deployments.gauss_campaign_runtime import (
    build_gauss_campaign_analysis,
)
from leo_flow.deployments.gauss_staged_analysis_runtime import (
    GaussCampaignScopedAnalysisWorkerV1,
)
from leo_flow.deployments.process_mode_lock import ExclusiveModeLock
from leo_flow.deployments.recording_submission_v1 import analysis_connection_factory
from leo_flow.deployments.staged_analysis_pool import (
    BoundedSpawnDeferredAnalysisLaneV1,
)
from leo_flow.hardware.linkage import RecordingHardwareLinker
from leo_flow.hardware.persistence import (
    DurableHardwareMetadataRepository,
    HardwareSnapshotNotFoundError,
)
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from leo_flow.services.capture_batch_analysis import (
    ClosedBatchAnalysisSelection,
    ClosedBatchAnalysisSubmissionService,
)
from leo_flow.services.config import AnalysisServiceConfig
from leo_flow.services.starlink_suite_submission import (
    StarlinkSuiteAnalysisSubmissionServiceV0_2,
    StarlinkSuiteAnalysisSubmissionV0_2,
)
from leo_flow.services.waterfall_submission import (
    WaterfallAnalysisSubmissionServiceV0_1,
    WaterfallAnalysisSubmissionV0_1,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
from leo_flow.storage.recording_codec import SigMFRecordingObjectReader
from leo_station.analysis_v1 import (
    CAS_ROOT,
    MODE_LOCK_PATH,
    RECORDING_ALGORITHM_REF,
    RECORDING_CONFIG_REF,
    RECORDING_DEPENDENCY_REFS,
    STARLINK_SUITE_ALGORITHM_REF,
    WATERFALL_ALGORITHM_REF,
    WATERFALL_CONFIG_REF,
    WATERFALL_DEPENDENCY_REFS,
    starlink_suite_profile_v0_2,
)

MAXIMUM_FOCUSED_COMPUTE_WORKERS = 8
MAXIMUM_FOCUSED_PROJECTION_WORKERS = 1
_LOG = logging.getLogger(__name__)


class _RecordingHardwareLinkerPort(Protocol):
    def link(self, recording_id: RecordingId) -> object: ...


@dataclass(frozen=True, slots=True)
class FocusedAnalysisJobScopeV1:
    """The six exact durable jobs belonging to one synchronized radio pair."""

    feature_job_ids: tuple[JobId, JobId]
    waterfall_job_ids: tuple[JobId, JobId]
    starlink_suite_job_ids: tuple[JobId, JobId]
    first_success_index: int = 0

    def __post_init__(self) -> None:
        all_ids = (
            *self.feature_job_ids,
            *self.waterfall_job_ids,
            *self.starlink_suite_job_ids,
        )
        if len(set(all_ids)) != 6:
            raise ValueError("focused analysis job identities must be unique")


def focused_stage_worker_count(
    stage: DeferredAnalysisStage,
    requested_compute_workers: int,
) -> int:
    """Return the useful bounded process count for a two-recording pair."""

    if not 1 <= requested_compute_workers <= MAXIMUM_FOCUSED_COMPUTE_WORKERS:
        raise ValueError("focused compute workers must be within 1..8")
    if stage.value.endswith("compute"):
        return min(2, requested_compute_workers)
    return MAXIMUM_FOCUSED_PROJECTION_WORKERS


def analyze_focused_pair(
    snapshot: CaptureBatchSnapshot,
    config: AnalysisServiceConfig,
    analysis_credential_directory: Path,
    dashboard_credential_directory: Path,
    *,
    deadline_utc_ns: UtcNs,
    compute_workers: int = MAXIMUM_FOCUSED_COMPUTE_WORKERS,
    capture_definition_digest: Digest | None = None,
    capture_safe: bool = False,
) -> CampaignAnalysisReceipt:
    """Submit, drain, project, and prove one exact pair without radio contact."""

    if len(snapshot.successful_recordings) != 2 or not snapshot.terminal:
        raise ValueError("focused analysis requires one terminal successful pair")
    if capture_safe and capture_definition_digest is None:
        raise ValueError("capture-safe focused analysis requires definition digest")
    scope = _prepare_scope(
        snapshot,
        analysis_credential_directory,
        capture_definition_digest=(capture_definition_digest if capture_safe else None),
    )
    credentials = SystemdCredentialProvider(analysis_credential_directory)
    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    lane = BoundedSpawnDeferredAnalysisLaneV1(
        GaussCampaignScopedAnalysisWorkerV1(analysis_credential_directory),
        PostgresCampaignAnalysisLaneStateReaderV1(connect),
    )
    lock = None if capture_safe else ExclusiveModeLock(MODE_LOCK_PATH)
    if lock is not None:
        lock.acquire()
    try:
        window = cast(DeferredAnalysisWindowV1, scope)
        for stage in DeferredAnalysisStage:
            result = lane.drain(
                window,
                stage,
                workers=focused_stage_worker_count(stage, compute_workers),
                deadline_utc_ns=deadline_utc_ns,
            )
            if (
                result.state is not DeferredAnalysisLaneState.COMPLETE
                or result.expected_count != 2
                or result.succeeded_count != 2
            ):
                raise RuntimeError(
                    f"focused analysis stage did not close: {stage.value}"
                )
        return build_gauss_campaign_analysis(
            config,
            analysis_credential_directory,
            dashboard_credential_directory,
            lock_analysis=False,
        ).analyze(snapshot, deadline_utc_ns=deadline_utc_ns)
    finally:
        if lock is not None:
            lock.release()


def _prepare_scope(
    snapshot: CaptureBatchSnapshot,
    credential_directory: Path,
    *,
    capture_definition_digest: Digest | None = None,
) -> FocusedAnalysisJobScopeV1:
    credentials = SystemdCredentialProvider(credential_directory)
    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    jobs = PostgresJobLeaseRepository(connect)
    recordings = PostgresRecordingCatalog(connect)
    blobs = FileSystemBlobStore(CAS_ROOT)
    reader = SigMFRecordingObjectReader(blobs)
    hardware = DurableHardwareMetadataRepository(
        blobs, PostgresHardwareSnapshotCatalog(connect)
    )
    hardware_linker = RecordingHardwareLinker(
        recordings,
        reader,
        hardware,
        hardware,
        PostgresRecordingHardwareLinkCatalog(connect),
    )
    CaptureBatchDashboardPublisher(
        PostgresCaptureBatchProjectionWriter(connect)
    ).publish_initial(snapshot)
    feature_submission = ClosedBatchAnalysisSubmissionService(recordings, jobs)
    waterfall_submission = WaterfallAnalysisSubmissionServiceV0_1(jobs)
    suite_submission = StarlinkSuiteAnalysisSubmissionServiceV0_2(jobs)
    selection = ClosedBatchAnalysisSelection(
        RECORDING_ALGORITHM_REF,
        RECORDING_CONFIG_REF,
        RECORDING_DEPENDENCY_REFS,
        SchemaRef(FeatureSetBundle.SCHEMA_ID),
    )
    ordered_recordings = tuple(
        sorted(snapshot.successful_recordings, key=lambda item: str(item.recording_id))
    )
    _link_focused_recording_hardware(
        tuple(item.recording_id for item in ordered_recordings), hardware_linker
    )
    submitted = feature_submission.submit(snapshot, selection)
    feature_by_recording = {
        item.request.recording_id: item.job_id for item in submitted.recording_jobs
    }
    feature: list[JobId] = []
    waterfalls: list[JobId] = []
    suites: list[JobId] = []
    for recording in ordered_recordings:
        recording_id = recording.recording_id
        feature.append(feature_by_recording[recording_id])
        waterfalls.append(
            waterfall_submission.submit(
                WaterfallAnalysisSubmissionV0_1(
                    recording,
                    WATERFALL_ALGORITHM_REF,
                    WATERFALL_CONFIG_REF,
                    WATERFALL_DEPENDENCY_REFS,
                )
            ).job_id
        )
        with reader.open(recording.recording_object) as view:
            rates = {
                segment.actual_sample_rate_hz for segment in view.manifest.segments
            }
            if len(rates) != 1:
                raise RuntimeError("focused recording mixes actual sample rates")
            profile = starlink_suite_profile_v0_2(next(iter(rates)))
            manifest = view.manifest
        suites.append(
            suite_submission.submit(
                StarlinkSuiteAnalysisSubmissionV0_2(
                    recording,
                    manifest,
                    STARLINK_SUITE_ALGORITHM_REF,
                    profile.config_ref,
                    profile.probe_sample_count,
                )
            ).job_id
        )
    scope = FocusedAnalysisJobScopeV1(
        _pair(feature, "feature"),
        _pair(waterfalls, "waterfall"),
        _pair(suites, "Starlink-suite"),
    )
    if capture_definition_digest is not None:
        ordered = tuple(
            sorted(
                snapshot.successful_recordings,
                key=lambda item: str(item.recording_id),
            )
        )
        if len(ordered) != 2:
            raise RuntimeError("focused registration requires two recordings")
        PostgresFocusedAnalysisPairScopeRegistrarV0_1(connect).register(
            FocusedAnalysisPairScopeV0_1(
                capture_definition_digest,
                snapshot.batch_id,
                (ordered[0].recording_id, ordered[1].recording_id),
                (
                    ordered[0].recording_object.identity_digest(),
                    ordered[1].recording_object.identity_digest(),
                ),
                scope.feature_job_ids,
                scope.waterfall_job_ids,
                scope.starlink_suite_job_ids,
            )
        )
    return scope


def _link_focused_recording_hardware(
    recording_ids: tuple[RecordingId, ...],
    linker: _RecordingHardwareLinkerPort,
) -> None:
    """Freeze each focused recording's exact manifest hardware before jobs exist."""

    if len(recording_ids) != 2 or len(set(recording_ids)) != 2:
        raise RuntimeError("focused hardware linkage requires two recordings")
    for recording_id in sorted(recording_ids, key=str):
        try:
            linker.link(recording_id)
        except HardwareSnapshotNotFoundError:
            _LOG.warning(
                "focused recording hardware snapshot is not cataloged; "
                "continuing analysis without hardware linkage",
                extra={"recording_id": str(recording_id)},
            )


def _pair(values: list[JobId], name: str) -> tuple[JobId, JobId]:
    if len(values) != 2 or len(set(values)) != 2:
        raise RuntimeError(f"focused {name} submission did not return two exact jobs")
    return values[0], values[1]
