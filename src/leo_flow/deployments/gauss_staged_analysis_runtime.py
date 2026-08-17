"""Gauss composition for campaign-scoped staged deferred analysis."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import psycopg

from leo_flow.adapters.campaign_online_analysis_postgres import (
    PostgresCampaignAnalysisScopeRegistrarV1,
)
from leo_flow.adapters.campaign_scoped_claims_postgres import (
    PostgresCampaignAnalysisLaneStateReaderV1,
    PostgresCampaignScopedJobClaimsV1,
)
from leo_flow.adapters.dashboard_batch_postgres import (
    PostgresCaptureBatchProjectionWriter,
)
from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.application.capture_batch_dashboard import (
    CaptureBatchDashboardPublisher,
)
from leo_flow.application.deferred_analysis import (
    DeferredAnalysisWorkerPolicyV1,
    ExactDeferredAnalysisWindowCoordinatorV1,
    OnlineDeferredAnalysisWindowCoordinatorV1,
)
from leo_flow.capture.campaign import CampaignDefinition
from leo_flow.capture.continuous import (
    ContinuousCollectionStatus,
    DeferredCampaignCoordinator,
)
from leo_flow.contracts.capture_batch import CaptureBatchSnapshot
from leo_flow.contracts.core import JobId, SchemaRef, UtcNs, canonical_digest
from leo_flow.contracts.deferred_analysis import (
    DeferredAnalysisCampaignDefinitionV1,
    DeferredAnalysisStage,
    DeferredAnalysisWindowV1,
)
from leo_flow.contracts.features import FeatureSetBundle
from leo_flow.deployments.recording_submission_v1 import analysis_connection_factory
from leo_flow.deployments.staged_analysis_pool import (
    BoundedSpawnDeferredAnalysisLaneV1,
)
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from leo_flow.services.capture_batch_analysis import (
    ClosedBatchAnalysisSelection,
    ClosedBatchAnalysisSubmissionService,
)
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
    RECORDING_ALGORITHM_REF,
    RECORDING_CONFIG_REF,
    RECORDING_DEPENDENCY_REFS,
    SCIENTIFIC,
    STARLINK_SUITE_ALGORITHM_REF,
    STARLINK_SUITE_ANALYZER,
    WATERFALL_ALGORITHM_REF,
    WATERFALL_ANALYZER,
    WATERFALL_CONFIG_REF,
    WATERFALL_DEPENDENCY_REFS,
    starlink_suite_profile_v0_2,
)

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class GaussDeferredAnalysisWindowPreparerV1:
    def __init__(self, credential_directory: Path) -> None:
        self._credential_directory = credential_directory

    def prepare(
        self,
        definition: DeferredAnalysisCampaignDefinitionV1,
        first_success_index: int,
        snapshots: tuple[CaptureBatchSnapshot, ...],
    ) -> DeferredAnalysisWindowV1:
        credentials = SystemdCredentialProvider(self._credential_directory)
        connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
        jobs = PostgresJobLeaseRepository(connect)
        recordings = PostgresRecordingCatalog(connect)
        feature_submission = ClosedBatchAnalysisSubmissionService(recordings, jobs)
        waterfall_submission = WaterfallAnalysisSubmissionServiceV0_1(jobs)
        suite_submission = StarlinkSuiteAnalysisSubmissionServiceV0_2(jobs)
        batch_projection = PostgresCaptureBatchProjectionWriter(connect)
        selection = ClosedBatchAnalysisSelection(
            RECORDING_ALGORITHM_REF,
            RECORDING_CONFIG_REF,
            RECORDING_DEPENDENCY_REFS,
            SchemaRef(FeatureSetBundle.SCHEMA_ID),
        )
        recording_ids = []
        recording_identity_digests = []
        feature_jobs = []
        waterfall_jobs = []
        suite_jobs = []
        blobs = FileSystemBlobStore(CAS_ROOT)
        reader = SigMFRecordingObjectReader(blobs)
        for snapshot in snapshots:
            CaptureBatchDashboardPublisher(batch_projection).publish_initial(snapshot)
            submitted = feature_submission.submit(snapshot, selection)
            submitted_features = {
                item.request.recording_id: item.job_id
                for item in submitted.recording_jobs
            }
            for published in sorted(
                snapshot.successful_recordings,
                key=lambda item: str(item.recording_id),
            ):
                recording_ids.append(published.recording_id)
                recording_identity_digests.append(
                    canonical_digest(published.recording_object)
                )
                feature_jobs.append(submitted_features[published.recording_id])
                waterfall_jobs.append(
                    waterfall_submission.submit(
                        WaterfallAnalysisSubmissionV0_1(
                            published,
                            WATERFALL_ALGORITHM_REF,
                            WATERFALL_CONFIG_REF,
                            WATERFALL_DEPENDENCY_REFS,
                        )
                    ).job_id
                )
                with reader.open(published.recording_object) as view:
                    rates = {
                        segment.actual_sample_rate_hz
                        for segment in view.manifest.segments
                    }
                    if len(rates) != 1:
                        raise RuntimeError("detector-suite recording mixes rates")
                    profile = starlink_suite_profile_v0_2(next(iter(rates)))
                    manifest = view.manifest
                suite_jobs.append(
                    suite_submission.submit(
                        StarlinkSuiteAnalysisSubmissionV0_2(
                            published,
                            manifest,
                            STARLINK_SUITE_ALGORITHM_REF,
                            profile.config_ref,
                            profile.probe_sample_count,
                        )
                    ).job_id
                )
        window = DeferredAnalysisWindowV1(
            definition.digest,
            first_success_index,
            tuple(snapshot.batch_id for snapshot in snapshots),
            tuple(recording_ids),
            tuple(recording_identity_digests),
            tuple(feature_jobs),
            tuple(waterfall_jobs),
            tuple(suite_jobs),
        )
        PostgresCampaignAnalysisScopeRegistrarV1(connect).register(window)
        return window


@dataclass(frozen=True, slots=True)
class GaussCampaignScopedAnalysisWorkerV1:
    credential_directory: Path

    def process_one(
        self,
        stage: DeferredAnalysisStage,
        window: DeferredAnalysisWindowV1,
        worker_instance_id: str,
    ) -> bool:
        credentials = SystemdCredentialProvider(self.credential_directory)
        connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
        if stage is DeferredAnalysisStage.FEATURE_COMPUTE:
            return _feature_compute(connect, window, worker_instance_id)
        if stage is DeferredAnalysisStage.FEATURE_PROJECTION:
            return _feature_projection(connect, window, worker_instance_id)
        if stage is DeferredAnalysisStage.WATERFALL_COMPUTE:
            return _waterfall_compute(connect, window, worker_instance_id)
        if stage is DeferredAnalysisStage.WATERFALL_PROJECTION:
            return _waterfall_projection(connect, window, worker_instance_id)
        if stage is DeferredAnalysisStage.STARLINK_SUITE_COMPUTE:
            return _suite_compute(connect, window, worker_instance_id)
        return _suite_projection(connect, window, worker_instance_id)


class GaussDeferredAnalysisReceiptReconcilerV1:
    def __init__(self, coordinator: DeferredCampaignCoordinator) -> None:
        self._coordinator = coordinator

    def reconcile_one(self, *, deadline_utc_ns: UtcNs) -> bool:
        result = self._coordinator.analyze_next(deadline_utc_ns=deadline_utc_ns)
        if result.status is ContinuousCollectionStatus.HALTED:
            raise RuntimeError("exact campaign receipt reconciliation halted")
        return result.status is ContinuousCollectionStatus.ANALYZED


def build_gauss_staged_campaign_analysis(
    definition: CampaignDefinition,
    coordinator: DeferredCampaignCoordinator,
    compute_workers: int,
    projection_workers: int,
    analysis_credential_directory: Path,
) -> ExactDeferredAnalysisWindowCoordinatorV1:
    credentials = SystemdCredentialProvider(analysis_credential_directory)
    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    lane = BoundedSpawnDeferredAnalysisLaneV1(
        GaussCampaignScopedAnalysisWorkerV1(analysis_credential_directory),
        PostgresCampaignAnalysisLaneStateReaderV1(connect),
    )
    return ExactDeferredAnalysisWindowCoordinatorV1(
        DeferredAnalysisCampaignDefinitionV1(
            definition.digest,
            definition.qualification,
            definition.analysis_after_each_capture,
            definition.target_successes,
        ),
        GaussDeferredAnalysisWindowPreparerV1(analysis_credential_directory),
        lane,
        GaussDeferredAnalysisReceiptReconcilerV1(coordinator),
        DeferredAnalysisWorkerPolicyV1(compute_workers, projection_workers),
    )


def build_gauss_online_campaign_analysis(
    definition: CampaignDefinition,
    compute_workers: int,
    projection_workers: int,
    analysis_credential_directory: Path,
) -> OnlineDeferredAnalysisWindowCoordinatorV1:
    credentials = SystemdCredentialProvider(analysis_credential_directory)
    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    lane = BoundedSpawnDeferredAnalysisLaneV1(
        GaussCampaignScopedAnalysisWorkerV1(analysis_credential_directory),
        PostgresCampaignAnalysisLaneStateReaderV1(connect),
    )
    return OnlineDeferredAnalysisWindowCoordinatorV1(
        DeferredAnalysisCampaignDefinitionV1(
            definition.digest,
            definition.qualification,
            definition.analysis_after_each_capture,
            definition.target_successes,
        ),
        GaussDeferredAnalysisWindowPreparerV1(analysis_credential_directory),
        lane,
        DeferredAnalysisWorkerPolicyV1(compute_workers, projection_workers),
    )


def _claims(
    connect: ConnectionFactory,
    job_ids: Sequence[JobId],
    job_type: JobType,
    worker_id: str,
    ttl_s: float,
) -> JobLease | None:
    return PostgresCampaignScopedJobClaimsV1(connect).claim(
        job_ids, job_type, worker_id, ttl_s
    )


def _feature_compute(
    connect: ConnectionFactory,
    window: DeferredAnalysisWindowV1,
    worker_id: str,
) -> bool:
    from leo_flow.adapters.recording_analysis_postgres import (
        AtomicPostgresRecordingAnalysisCommitter,
    )
    from leo_flow.services.recording_analysis import (
        FencedRecordingAnalysisWorker,
        RecordingAnalysisJobPreparer,
    )

    lease = _claims(
        connect, window.feature_job_ids, JobType.RECORDING_ANALYSIS, worker_id, 900.0
    )
    if lease is None:
        return False
    jobs = PostgresJobLeaseRepository(connect)
    if lease.attempt > 3:
        jobs.park(
            lease.job_id,
            lease.lease_token,
            lease.lease_generation,
            "recording-analysis-attempts-exhausted",
        )
        return True
    analyzers = tuple(SCIENTIFIC.recording_analyzers.values())
    if len(analyzers) != 1:
        raise RuntimeError("Gauss recording analyzer registry is ambiguous")
    blobs = FileSystemBlobStore(CAS_ROOT)
    try:
        FencedRecordingAnalysisWorker(
            jobs,
            RecordingAnalysisJobPreparer(
                SigMFRecordingObjectReader(blobs), analyzers[0]
            ),
            AtomicPostgresRecordingAnalysisCommitter(blobs, connect),
            worker_id=worker_id,
            lease_ttl_s=900.0,
        ).execute(lease)
    except Exception:  # noqa: BLE001 - worker durably records retryable failure
        return True
    return True


def _feature_projection(
    connect: ConnectionFactory,
    window: DeferredAnalysisWindowV1,
    worker_id: str,
) -> bool:
    from leo_flow.adapters.dashboard_batch_postgres import (
        PostgresBatchAwareAnalysisProjectionWriter,
    )
    from leo_flow.adapters.dashboard_projection_postgres import (
        PostgresAnalysisProjectionWriter,
    )
    from leo_flow.adapters.feature_postgres_catalog import PostgresFeatureSetCatalog
    from leo_flow.adapters.feature_projection_work_postgres import (
        PostgresFeatureProjectionWorkRepository,
    )
    from leo_flow.analysis.recording import DurableFeatureSetRepository
    from leo_flow.application.feature_projection_work import FeatureProjectionWorker

    blobs = FileSystemBlobStore(CAS_ROOT)
    source_job_ids = _batch_affine_feature_projection_jobs(window, worker_id)
    try:
        return FeatureProjectionWorker(
            PostgresFeatureProjectionWorkRepository(
                connect, source_job_ids=source_job_ids
            ),
            DurableFeatureSetRepository(blobs, PostgresFeatureSetCatalog(connect)),
            PostgresRecordingCatalog(connect),
            PostgresBatchAwareAnalysisProjectionWriter(
                connect, PostgresAnalysisProjectionWriter(connect)
            ),
            worker_id=worker_id,
            lease_ttl_s=60.0,
            retry_delay_s=5.0,
        ).process_one_work()
    except Exception:  # noqa: BLE001 - projection records a retryable failure
        return True


def _batch_affine_feature_projection_jobs(
    window: DeferredAnalysisWindowV1, worker_id: str
) -> tuple[JobId, ...]:
    """Keep both recordings from a batch on one projection worker."""

    marker = worker_id.rpartition("-")[2]
    prefix, separator, total_value = worker_id.rpartition("-of-")
    if not separator:
        raise RuntimeError("campaign worker shard identity is malformed")
    index_value = prefix.rpartition("-")[2]
    if marker != total_value:
        raise RuntimeError("campaign worker shard identity is malformed")
    try:
        index, total = int(index_value), int(total_value)
    except ValueError as error:
        raise RuntimeError("campaign worker shard identity is malformed") from error
    if not 1 <= index <= total <= 4:
        raise RuntimeError("campaign worker shard identity is out of bounds")
    pairs = tuple(
        window.feature_job_ids[offset : offset + 2]
        for offset in range(0, len(window.feature_job_ids), 2)
    )
    selected = tuple(
        job_id
        for batch_index, pair in enumerate(pairs)
        if batch_index % total == index - 1
        for job_id in pair
    )
    if not selected:
        raise RuntimeError("campaign worker shard has no feature jobs")
    return selected


def _waterfall_compute(
    connect: ConnectionFactory,
    window: DeferredAnalysisWindowV1,
    worker_id: str,
) -> bool:
    from leo_flow.adapters.waterfall_analysis_postgres import (
        AtomicPostgresWaterfallCommitterV0_1,
    )
    from leo_flow.services.waterfall_analysis import (
        FencedWaterfallAnalysisWorkerV0_1,
        WaterfallAnalysisJobPreparerV0_1,
    )

    lease = _claims(
        connect, window.waterfall_job_ids, JobType.WATERFALL_ANALYSIS, worker_id, 900.0
    )
    if lease is None:
        return False
    blobs = FileSystemBlobStore(CAS_ROOT)
    FencedWaterfallAnalysisWorkerV0_1(
        PostgresJobLeaseRepository(connect),
        WaterfallAnalysisJobPreparerV0_1(
            SigMFRecordingObjectReader(blobs), WATERFALL_ANALYZER
        ),
        AtomicPostgresWaterfallCommitterV0_1(blobs, connect),
        worker_id=worker_id,
        lease_ttl_s=900.0,
    ).execute(lease)
    return True


def _waterfall_projection(
    connect: ConnectionFactory,
    window: DeferredAnalysisWindowV1,
    worker_id: str,
) -> bool:
    from leo_flow.adapters.dashboard_recording_postgres import (
        PostgresRecordingWaterfallProjectionWriter,
    )
    from leo_flow.adapters.waterfall_dashboard_projection_postgres import (
        PostgresWaterfallProjectionWorkRepositoryV0_1,
    )
    from leo_flow.adapters.waterfall_postgres_catalog import (
        PostgresWaterfallCatalogV0_1,
    )
    from leo_flow.analysis.recording.waterfall_persistence import (
        DurableWaterfallRepositoryV0_1,
    )
    from leo_flow.application.waterfall_projection_work import (
        WaterfallDashboardProjectionWorkerV0_1,
    )

    blobs = FileSystemBlobStore(CAS_ROOT)
    return WaterfallDashboardProjectionWorkerV0_1(
        PostgresWaterfallProjectionWorkRepositoryV0_1(
            connect, source_job_ids=window.waterfall_job_ids
        ),
        DurableWaterfallRepositoryV0_1(blobs, PostgresWaterfallCatalogV0_1(connect)),
        PostgresRecordingWaterfallProjectionWriter(connect),
        worker_id=worker_id,
        lease_ttl_s=60.0,
    ).process_one_work()


def _suite_compute(
    connect: ConnectionFactory,
    window: DeferredAnalysisWindowV1,
    worker_id: str,
) -> bool:
    from leo_flow.adapters.starlink_suite_postgres import (
        AtomicPostgresStarlinkSuiteCommitterV0_2,
    )
    from leo_flow.services.starlink_suite_analysis import (
        FencedStarlinkSuiteAnalysisWorkerV0_2,
        StarlinkSuiteAnalysisJobPreparerV0_2,
    )

    lease = _claims(
        connect,
        window.starlink_suite_job_ids,
        JobType.STARLINK_SUITE_ANALYSIS,
        worker_id,
        900.0,
    )
    if lease is None:
        return False
    blobs = FileSystemBlobStore(CAS_ROOT)
    FencedStarlinkSuiteAnalysisWorkerV0_2(
        PostgresJobLeaseRepository(connect),
        StarlinkSuiteAnalysisJobPreparerV0_2(
            SigMFRecordingObjectReader(blobs), STARLINK_SUITE_ANALYZER
        ),
        AtomicPostgresStarlinkSuiteCommitterV0_2(blobs, connect),
        worker_id=worker_id,
        lease_ttl_s=900.0,
    ).execute(lease)
    return True


def _suite_projection(
    connect: ConnectionFactory,
    window: DeferredAnalysisWindowV1,
    worker_id: str,
) -> bool:
    from leo_flow.adapters.dashboard_recording_postgres import (
        PostgresRecordingStarlinkSuiteProjectionWriterV0_2,
    )
    from leo_flow.adapters.starlink_suite_postgres import (
        PostgresStarlinkSuiteCatalogV0_2,
        PostgresStarlinkSuiteProjectionWorkRepositoryV0_2,
    )
    from leo_flow.analysis.recording.starlink_suite_persistence import (
        DurableStarlinkSuiteStoreV0_2,
    )
    from leo_flow.application.starlink_suite_projection_work import (
        StarlinkSuiteDashboardProjectionWorkerV0_2,
    )

    blobs = FileSystemBlobStore(CAS_ROOT)
    return StarlinkSuiteDashboardProjectionWorkerV0_2(
        PostgresStarlinkSuiteProjectionWorkRepositoryV0_2(
            connect, source_job_ids=window.starlink_suite_job_ids
        ),
        DurableStarlinkSuiteStoreV0_2(blobs, PostgresStarlinkSuiteCatalogV0_2(connect)),
        PostgresRecordingStarlinkSuiteProjectionWriterV0_2(connect),
        worker_id=worker_id,
        lease_ttl_s=60.0,
    ).process_one_work()
