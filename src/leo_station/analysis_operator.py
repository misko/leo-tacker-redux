"""Machine-readable, explicitly invoked post-capture analysis operator."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from enum import IntEnum
from pathlib import Path
from typing import Any, NoReturn, Protocol, TextIO, cast

from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.application.capture_batch_dashboard import (
    CaptureBatchDashboardProjectionWriter,
    CaptureBatchDashboardPublisher,
)
from leo_flow.contracts.capture_batch import CaptureBatchSnapshot
from leo_flow.contracts.capture_batch_codec import decode_capture_batch_snapshot
from leo_flow.contracts.core import (
    JobId,
    RecordingId,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.features import FeatureSetBundle
from leo_flow.deployments.offline_analysis_v1 import (
    DATABASE_SECRET,
    FEATURE_PUBLISHER_REF,
    JOB_REPOSITORY_REF,
    MODEL_PUBLISHER_REF,
    RECORDING_READER_REF,
)
from leo_flow.deployments.process_mode_lock import ExclusiveModeLock
from leo_flow.deployments.recording_submission_v1 import (
    analysis_connection_factory,
    submit_recording_analysis,
)
from leo_flow.services.bootstrap import (
    DeploymentPlugin,
    SecretProvider,
    assemble_service,
)
from leo_flow.services.capture_batch_analysis import (
    ClosedBatchAnalysisSelection,
    ClosedBatchAnalysisSubmissionService,
    PublishedRecordingCatalog,
    SubmittedClosedBatchAnalysis,
)
from leo_flow.services.config import (
    AnalysisServiceConfig,
    ConfigurationError,
    load_service_config,
)
from leo_flow.services.lifecycle import JsonLineDiagnosticSink
from leo_flow.services.recording_submission import (
    RecordingAnalysisJobEnqueuer,
    SubmittedRecordingAnalysis,
)
from leo_flow.services.recording_submission_operator import (
    ExactRecordingAnalysisSelection,
    RecordingSubmissionOperatorConfig,
)
from leo_flow.services.starlink_submission import SubmittedStarlinkAnalysisV0_1
from leo_flow.services.starlink_suite_submission import (
    SubmittedStarlinkSuiteAnalysisV0_2,
)
from leo_flow.services.waterfall_submission import SubmittedWaterfallAnalysisV0_1

from .analysis_v1 import (
    ACQUIRED_QAM_MAXIMUM_WINDOWS_PER_STREAM,
    CAS_ROOT,
    MODE_LOCK_PATH,
    PLUGIN,
    RECORDING_ALGORITHM_REF,
    RECORDING_CONFIG_REF,
    RECORDING_DEPENDENCY_REFS,
    SCIENCE_MANIFEST_DIGEST,
    STARLINK_ALGORITHM_REF,
    STARLINK_ANALYZER,
    STARLINK_PILOT_CONSTELLATION_ANALYZER,
    STARLINK_SUITE_ALGORITHM_REF,
    STARLINK_SUITE_ANALYZER,
    WATERFALL_ALGORITHM_REF,
    WATERFALL_ANALYZER,
    WATERFALL_CONFIG_REF,
    WATERFALL_DEPENDENCY_REFS,
    GaussRuntimeApprovalError,
    require_approved_runtime,
    science_manifest,
    starlink_acquired_dwell_profiles_v0_3,
    starlink_search_profile_v0_1,
    starlink_suite_profile_v0_2,
    starlink_surrogate_null_preparers_v0_1,
    starlink_temporal_pilot_preparers_v0_1,
)

MAX_MANIFEST_BYTES = 64 * 1024
MAX_BATCH_SNAPSHOT_BYTES = 1024 * 1024


class ExitCode(IntEnum):
    OK = 0
    USAGE_OR_CONFIG = 2
    SUBMISSION_FAILED = 3
    ANALYSIS_FAILED = 4


class GaussAnalysisOperatorError(RuntimeError):
    """Operator input differs from the checked Gauss analysis approval."""


class Submitter(Protocol):
    def __call__(
        self,
        config: RecordingSubmissionOperatorConfig,
        credentials: SecretProvider,
    ) -> SubmittedRecordingAnalysis: ...


class Processor(Protocol):
    def __call__(
        self,
        config: AnalysisServiceConfig,
        credentials: SecretProvider,
        stdout: TextIO,
    ) -> bool: ...


class Projector(Protocol):
    def __call__(self, credentials: SecretProvider) -> bool: ...


class BatchSubmitter(Protocol):
    def __call__(
        self,
        snapshot: CaptureBatchSnapshot,
        selection: ClosedBatchAnalysisSelection,
        credentials: SecretProvider,
    ) -> SubmittedClosedBatchAnalysis: ...


class WaterfallSubmitter(Protocol):
    def __call__(
        self, recording_id: RecordingId, credentials: SecretProvider
    ) -> SubmittedWaterfallAnalysisV0_1: ...


class WaterfallProcessor(Protocol):
    def __call__(self, credentials: SecretProvider) -> bool: ...


class WaterfallProjector(Protocol):
    def __call__(self, credentials: SecretProvider) -> bool: ...


class StarlinkSubmitter(Protocol):
    def __call__(
        self, recording_id: RecordingId, credentials: SecretProvider
    ) -> SubmittedStarlinkAnalysisV0_1: ...


class StarlinkProcessor(Protocol):
    def __call__(self, credentials: SecretProvider) -> bool: ...


class StarlinkProjector(Protocol):
    def __call__(self, credentials: SecretProvider) -> bool: ...


class StarlinkSuiteSubmitter(Protocol):
    def __call__(
        self, recording_id: RecordingId, credentials: SecretProvider
    ) -> SubmittedStarlinkSuiteAnalysisV0_2: ...


class StarlinkSuiteProcessor(Protocol):
    def __call__(self, credentials: SecretProvider) -> bool: ...


class StarlinkSuiteProjector(Protocol):
    def __call__(self, credentials: SecretProvider) -> bool: ...


class AcquiredQamBackfiller(Protocol):
    def __call__(
        self, recording_id: RecordingId, credentials: SecretProvider
    ) -> object: ...


class _ModeLock(Protocol):
    def acquire(self) -> None: ...

    def release(self) -> None: ...


ModeLockFactory = Callable[[Path], _ModeLock]
CredentialFactory = Callable[[Path], SecretProvider]


def _submit(
    config: RecordingSubmissionOperatorConfig, credentials: SecretProvider
) -> SubmittedRecordingAnalysis:
    return submit_recording_analysis(config, credentials=credentials)


def _submit_closed_batch(
    snapshot: CaptureBatchSnapshot,
    selection: ClosedBatchAnalysisSelection,
    credentials: SecretProvider,
) -> SubmittedClosedBatchAnalysis:
    try:
        from leo_flow.adapters.dashboard_batch_postgres import (
            PostgresCaptureBatchProjectionWriter,
        )
        from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
        from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
    except ImportError as error:
        raise GaussAnalysisOperatorError(
            "closed-batch submission requires the server dependency"
        ) from error
    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    return _submit_closed_batch_with_ports(
        snapshot,
        selection,
        PostgresRecordingCatalog(connect),
        PostgresJobLeaseRepository(connect),
        PostgresCaptureBatchProjectionWriter(connect),
    )


def _submit_closed_batch_with_ports(
    snapshot: CaptureBatchSnapshot,
    selection: ClosedBatchAnalysisSelection,
    recordings: PublishedRecordingCatalog,
    jobs: RecordingAnalysisJobEnqueuer,
    batch_projection: CaptureBatchDashboardProjectionWriter,
) -> SubmittedClosedBatchAnalysis:
    CaptureBatchDashboardPublisher(batch_projection).publish_initial(snapshot)
    return ClosedBatchAnalysisSubmissionService(recordings, jobs).submit(
        snapshot, selection
    )


def _process_one(
    config: AnalysisServiceConfig,
    credentials: SecretProvider,
    stdout: TextIO,
) -> bool:
    plugin = DeploymentPlugin(
        PLUGIN.manifest,
        {"systemd-credential": credentials},
        PLUGIN.builders,
    )
    service = assemble_service(
        config, plugin, diagnostics=JsonLineDiagnosticSink(stdout)
    )
    try:
        return service.run_once()
    finally:
        service.shutdown()


def _project_one(credentials: SecretProvider) -> bool:
    """Compose the dedicated durable outbox worker from public readers/ports."""

    try:
        from leo_flow.adapters.dashboard_batch_postgres import (
            PostgresBatchAwareAnalysisProjectionWriter,
        )
        from leo_flow.adapters.dashboard_projection_postgres import (
            PostgresAnalysisProjectionWriter,
        )
        from leo_flow.adapters.feature_postgres_catalog import (
            PostgresFeatureSetCatalog,
        )
        from leo_flow.adapters.feature_projection_work_postgres import (
            PostgresFeatureProjectionWorkRepository,
        )
        from leo_flow.analysis.recording import DurableFeatureSetRepository
        from leo_flow.application.feature_projection_work import (
            FeatureProjectionWorker,
        )
        from leo_flow.storage.filesystem import FileSystemBlobStore
        from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
    except ImportError as error:
        raise GaussAnalysisOperatorError(
            "feature projection requires the server dependency"
        ) from error

    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    blobs = FileSystemBlobStore(CAS_ROOT)
    worker = FeatureProjectionWorker(
        PostgresFeatureProjectionWorkRepository(connect),
        DurableFeatureSetRepository(blobs, PostgresFeatureSetCatalog(connect)),
        PostgresRecordingCatalog(connect),
        PostgresBatchAwareAnalysisProjectionWriter(
            connect, PostgresAnalysisProjectionWriter(connect)
        ),
        worker_id="gauss-feature-projection-1",
        lease_ttl_s=60.0,
        retry_delay_s=5.0,
    )
    return worker.process_one_work()


def _submit_waterfall(
    recording_id: RecordingId, credentials: SecretProvider
) -> SubmittedWaterfallAnalysisV0_1:
    try:
        from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
        from leo_flow.services.waterfall_submission import (
            WaterfallAnalysisSubmissionServiceV0_1,
            WaterfallAnalysisSubmissionV0_1,
        )
        from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
    except ImportError as error:
        raise GaussAnalysisOperatorError(
            "waterfall submission requires the server dependency"
        ) from error
    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    recording = PostgresRecordingCatalog(connect).get(recording_id)
    if recording is None or recording.recording_id != recording_id:
        raise GaussAnalysisOperatorError("published waterfall input was not found")
    return WaterfallAnalysisSubmissionServiceV0_1(
        PostgresJobLeaseRepository(connect)
    ).submit(
        WaterfallAnalysisSubmissionV0_1(
            recording,
            WATERFALL_ALGORITHM_REF,
            WATERFALL_CONFIG_REF,
            WATERFALL_DEPENDENCY_REFS,
        )
    )


def _process_waterfall_one(credentials: SecretProvider) -> bool:
    try:
        from leo_flow.adapters.waterfall_analysis_postgres import (
            AtomicPostgresWaterfallCommitterV0_1,
        )
        from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
        from leo_flow.services.waterfall_analysis import (
            FencedWaterfallAnalysisWorkerV0_1,
            WaterfallAnalysisJobPreparerV0_1,
        )
        from leo_flow.storage.filesystem import FileSystemBlobStore
        from leo_flow.storage.recording_codec import SigMFRecordingObjectReader
    except ImportError as error:
        raise GaussAnalysisOperatorError(
            "waterfall processing requires the server dependency"
        ) from error
    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    blobs = FileSystemBlobStore(CAS_ROOT)
    jobs = PostgresJobLeaseRepository(connect)
    worker = FencedWaterfallAnalysisWorkerV0_1(
        jobs,
        WaterfallAnalysisJobPreparerV0_1(
            SigMFRecordingObjectReader(blobs), WATERFALL_ANALYZER
        ),
        AtomicPostgresWaterfallCommitterV0_1(blobs, connect),
        worker_id="gauss-waterfall-analysis-1",
        lease_ttl_s=60.0,
    )
    return worker.process_one_job()


def _project_waterfall_one(credentials: SecretProvider) -> bool:
    try:
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
        from leo_flow.storage.filesystem import FileSystemBlobStore
    except ImportError as error:
        raise GaussAnalysisOperatorError(
            "waterfall projection requires the server dependency"
        ) from error
    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    blobs = FileSystemBlobStore(CAS_ROOT)
    worker = WaterfallDashboardProjectionWorkerV0_1(
        PostgresWaterfallProjectionWorkRepositoryV0_1(connect),
        DurableWaterfallRepositoryV0_1(
            blobs,
            PostgresWaterfallCatalogV0_1(connect),
        ),
        PostgresRecordingWaterfallProjectionWriter(connect),
        worker_id="gauss-waterfall-projection-1",
        lease_ttl_s=60.0,
    )
    return worker.process_one_work()


def _submit_starlink(
    recording_id: RecordingId, credentials: SecretProvider
) -> SubmittedStarlinkAnalysisV0_1:
    try:
        from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
        from leo_flow.services.starlink_submission import (
            StarlinkAnalysisSubmissionServiceV0_1,
            StarlinkAnalysisSubmissionV0_1,
            select_qin_starlink_streams_v0_1,
        )
        from leo_flow.storage.filesystem import FileSystemBlobStore
        from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
        from leo_flow.storage.recording_codec import SigMFRecordingObjectReader
    except ImportError as error:
        raise GaussAnalysisOperatorError(
            "Starlink submission requires the server dependency"
        ) from error
    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    recording = PostgresRecordingCatalog(connect).get(recording_id)
    if recording is None or recording.recording_id != recording_id:
        raise GaussAnalysisOperatorError("published Starlink input was not found")
    reader = SigMFRecordingObjectReader(FileSystemBlobStore(CAS_ROOT))
    with reader.open(recording.recording_object) as view:
        rates = {segment.actual_sample_rate_hz for segment in view.manifest.segments}
        if len(rates) != 1:
            raise GaussAnalysisOperatorError(
                "Starlink recording mixes actual sample rates"
            )
        profile = starlink_search_profile_v0_1(next(iter(rates)))
        selections = select_qin_starlink_streams_v0_1(
            view.manifest,
            sample_rate_hz=profile.sample_rate_hz,
            probe_sample_count=profile.probe_sample_count,
        )
    return StarlinkAnalysisSubmissionServiceV0_1(
        PostgresJobLeaseRepository(connect)
    ).submit(
        StarlinkAnalysisSubmissionV0_1(
            recording,
            STARLINK_ALGORITHM_REF,
            profile.config_ref,
            selections,
        )
    )


def _process_starlink_one(credentials: SecretProvider) -> bool:
    try:
        from leo_flow.adapters.starlink_analysis_postgres import (
            AtomicPostgresStarlinkCommitterV0_1,
        )
        from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
        from leo_flow.services.starlink_analysis import (
            FencedStarlinkAnalysisWorkerV0_1,
            StarlinkAnalysisJobPreparerV0_1,
        )
        from leo_flow.storage.filesystem import FileSystemBlobStore
        from leo_flow.storage.recording_codec import SigMFRecordingObjectReader
    except ImportError as error:
        raise GaussAnalysisOperatorError(
            "Starlink processing requires the server dependency"
        ) from error
    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    blobs = FileSystemBlobStore(CAS_ROOT)
    jobs = PostgresJobLeaseRepository(connect)
    worker = FencedStarlinkAnalysisWorkerV0_1(
        jobs,
        StarlinkAnalysisJobPreparerV0_1(
            SigMFRecordingObjectReader(blobs), STARLINK_ANALYZER
        ),
        AtomicPostgresStarlinkCommitterV0_1(blobs, connect),
        worker_id="gauss-starlink-analysis-1",
        lease_ttl_s=900.0,
    )
    return worker.process_one_job()


def _project_starlink_one(credentials: SecretProvider) -> bool:
    try:
        from leo_flow.adapters.dashboard_recording_postgres import (
            PostgresRecordingStarlinkProjectionWriter,
        )
        from leo_flow.adapters.starlink_postgres_catalog import (
            PostgresStarlinkCatalogV0_1,
        )
        from leo_flow.adapters.starlink_projection_postgres import (
            PostgresStarlinkProjectionWorkRepositoryV0_1,
        )
        from leo_flow.analysis.recording.starlink_persistence import (
            DurableStarlinkStoreV0_1,
        )
        from leo_flow.application.starlink_projection_work import (
            StarlinkDashboardProjectionWorkerV0_1,
        )
        from leo_flow.storage.filesystem import FileSystemBlobStore
    except ImportError as error:
        raise GaussAnalysisOperatorError(
            "Starlink projection requires the server dependency"
        ) from error
    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    blobs = FileSystemBlobStore(CAS_ROOT)
    worker = StarlinkDashboardProjectionWorkerV0_1(
        PostgresStarlinkProjectionWorkRepositoryV0_1(connect),
        DurableStarlinkStoreV0_1(blobs, PostgresStarlinkCatalogV0_1(connect)),
        PostgresRecordingStarlinkProjectionWriter(connect),
        worker_id="gauss-starlink-projection-1",
        lease_ttl_s=60.0,
    )
    return worker.process_one_work()


def _submit_starlink_suite(
    recording_id: RecordingId, credentials: SecretProvider
) -> SubmittedStarlinkSuiteAnalysisV0_2:
    from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
    from leo_flow.services.starlink_suite_submission import (
        StarlinkSuiteAnalysisSubmissionServiceV0_2,
        StarlinkSuiteAnalysisSubmissionV0_2,
    )
    from leo_flow.storage.filesystem import FileSystemBlobStore
    from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
    from leo_flow.storage.recording_codec import SigMFRecordingObjectReader

    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    recording = PostgresRecordingCatalog(connect).get(recording_id)
    if recording is None or recording.recording_id != recording_id:
        raise GaussAnalysisOperatorError("published detector-suite input was not found")
    with SigMFRecordingObjectReader(FileSystemBlobStore(CAS_ROOT)).open(
        recording.recording_object
    ) as view:
        rates = {segment.actual_sample_rate_hz for segment in view.manifest.segments}
        if len(rates) != 1:
            raise GaussAnalysisOperatorError("detector-suite recording mixes rates")
        profile = starlink_suite_profile_v0_2(next(iter(rates)))
        manifest = view.manifest
    return StarlinkSuiteAnalysisSubmissionServiceV0_2(
        PostgresJobLeaseRepository(connect)
    ).submit(
        StarlinkSuiteAnalysisSubmissionV0_2(
            recording,
            manifest,
            STARLINK_SUITE_ALGORITHM_REF,
            profile.config_ref,
            profile.probe_sample_count,
        )
    )


def _process_starlink_suite_one(credentials: SecretProvider) -> bool:
    from leo_flow.adapters.starlink_suite_postgres import (
        AtomicPostgresCombinedStarlinkSuiteCommitterV0_3,
    )
    from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
    from leo_flow.services.starlink_acquired_constellation_analysis import (
        CombinedStarlinkSuiteDwellAnalysisJobPreparerV0_3,
    )
    from leo_flow.services.starlink_suite_analysis import (
        FencedStarlinkSuiteAnalysisWorkerV0_2,
    )
    from leo_flow.services.starlink_suite_surrogate_analysis import (
        CombinedStarlinkSuiteAnalysisJobPreparerV0_2,
    )
    from leo_flow.storage.filesystem import FileSystemBlobStore
    from leo_flow.storage.recording_codec import SigMFRecordingObjectReader

    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    blobs = FileSystemBlobStore(CAS_ROOT)
    reader = SigMFRecordingObjectReader(blobs)
    return FencedStarlinkSuiteAnalysisWorkerV0_2(
        PostgresJobLeaseRepository(connect),
        CombinedStarlinkSuiteDwellAnalysisJobPreparerV0_3(
            reader,
            CombinedStarlinkSuiteAnalysisJobPreparerV0_2(
                reader,
                STARLINK_SUITE_ANALYZER,
                starlink_surrogate_null_preparers_v0_1(reader),
                STARLINK_PILOT_CONSTELLATION_ANALYZER,
                starlink_temporal_pilot_preparers_v0_1(),
            ),
            starlink_acquired_dwell_profiles_v0_3(),
            maximum_windows_per_stream=ACQUIRED_QAM_MAXIMUM_WINDOWS_PER_STREAM,
        ),
        AtomicPostgresCombinedStarlinkSuiteCommitterV0_3(blobs, connect),
        worker_id="gauss-starlink-suite-analysis-1",
        lease_ttl_s=900.0,
    ).process_one_job()


def _backfill_starlink_acquired_qam_v0_3(
    recording_id: RecordingId, credentials: SecretProvider
) -> object:
    """Publish only additive V17 evidence from an existing immutable suite."""
    from leo_flow.adapters.starlink_acquired_constellation_postgres import (
        PostgresStarlinkAcquiredConstellationCatalogV0_3,
    )
    from leo_flow.adapters.starlink_suite_postgres import (
        PostgresStarlinkSuiteCatalogV0_2,
    )
    from leo_flow.analysis.recording.starlink_acquired_constellation_persistence import (
        DurableStarlinkAcquiredConstellationStoreV0_3,
    )
    from leo_flow.analysis.recording.starlink_suite_persistence import (
        DurableStarlinkSuiteStoreV0_2,
    )
    from leo_flow.jobs.contracts import JobLease, JobType
    from leo_flow.services.starlink_acquired_constellation_analysis import (
        CombinedStarlinkSuiteDwellAnalysisJobPreparerV0_3,
    )
    from leo_flow.services.starlink_suite_submission import (
        StarlinkSuiteAnalysisSubmissionServiceV0_2,
        StarlinkSuiteAnalysisSubmissionV0_2,
    )
    from leo_flow.services.starlink_suite_surrogate_analysis import (
        PreparedCombinedStarlinkSuiteAnalysisV0_2,
    )
    from leo_flow.storage.filesystem import FileSystemBlobStore
    from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
    from leo_flow.storage.recording_codec import SigMFRecordingObjectReader

    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    blobs = FileSystemBlobStore(CAS_ROOT)
    recording = PostgresRecordingCatalog(connect).get(recording_id)
    if recording is None:
        raise GaussAnalysisOperatorError(
            "acquired-QAM backfill recording was not found"
        )
    reader = SigMFRecordingObjectReader(blobs)
    with reader.open(recording.recording_object) as view:
        rates = {segment.actual_sample_rate_hz for segment in view.manifest.segments}
        if len(rates) != 1:
            raise GaussAnalysisOperatorError("acquired-QAM recording mixes rates")
        profile = starlink_suite_profile_v0_2(next(iter(rates)))
        manifest = view.manifest

    class _NoEnqueue:
        def enqueue(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

    submitted = StarlinkSuiteAnalysisSubmissionServiceV0_2(
        cast(Any, _NoEnqueue())
    ).submit(
        StarlinkSuiteAnalysisSubmissionV0_2(
            recording,
            manifest,
            STARLINK_SUITE_ALGORITHM_REF,
            profile.config_ref,
            profile.probe_sample_count,
        )
    )
    suite_catalog = PostgresStarlinkSuiteCatalogV0_2(connect)
    source = suite_catalog.latest_starlink_suite(recording_id)
    if source is None or source.projection.request_digest != canonical_digest(
        submitted.request
    ):
        raise GaussAnalysisOperatorError(
            "acquired-QAM backfill has no exact source suite"
        )
    acquired_catalog = PostgresStarlinkAcquiredConstellationCatalogV0_3(connect)
    already = acquired_catalog.latest_starlink_acquired_constellation(recording_id)
    if already is not None:
        return already
    with DurableStarlinkSuiteStoreV0_2(blobs, suite_catalog).open(source.ref) as bundle:

        class _ExistingSuite:
            def prepare(
                self, lease: JobLease
            ) -> PreparedCombinedStarlinkSuiteAnalysisV0_2:
                del lease
                return PreparedCombinedStarlinkSuiteAnalysisV0_2(
                    submitted.request, bundle, cast(Any, None), None, None
                )

        preparer = CombinedStarlinkSuiteDwellAnalysisJobPreparerV0_3(
            reader,
            cast(Any, _ExistingSuite()),
            starlink_acquired_dwell_profiles_v0_3(),
            maximum_windows_per_stream=ACQUIRED_QAM_MAXIMUM_WINDOWS_PER_STREAM,
        )
        lease = JobLease(
            JobId(f"job_v03_backfill_{canonical_digest(submitted.request).value}"),
            JobType.STARLINK_SUITE_ANALYSIS,
            submitted.payload,
            1,
            "v03-backfill",
            1,
            UtcNs(time.time_ns() + 3_600_000_000_000),
        )
        acquired = preparer.prepare(lease).acquired_constellation_v0_3
    if acquired is None:
        raise GaussAnalysisOperatorError("source suite is not acquired-QAM eligible")
    return DurableStarlinkAcquiredConstellationStoreV0_3(
        blobs, acquired_catalog
    ).publish(
        acquired.request,
        acquired.bundle,
        idempotency_key=f"acquired-qam-v0.3-backfill:{recording_id}",
    )


def _project_starlink_suite_one(credentials: SecretProvider) -> bool:
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
    from leo_flow.storage.filesystem import FileSystemBlobStore

    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    blobs = FileSystemBlobStore(CAS_ROOT)
    return StarlinkSuiteDashboardProjectionWorkerV0_2(
        PostgresStarlinkSuiteProjectionWorkRepositoryV0_2(connect),
        DurableStarlinkSuiteStoreV0_2(blobs, PostgresStarlinkSuiteCatalogV0_2(connect)),
        PostgresRecordingStarlinkSuiteProjectionWriterV0_2(connect),
        worker_id="gauss-starlink-suite-projection-1",
        lease_ttl_s=60.0,
    ).process_one_work()


def load_approved_manifest(path: Path) -> dict[str, object]:
    """Require exact checked science bytes semantically, with duplicate rejection."""

    try:
        payload = path.read_bytes()
        if len(payload) > MAX_MANIFEST_BYTES:
            _bad("science manifest exceeds the size limit")
        value = json.loads(payload, object_pairs_hook=_unique)
    except GaussAnalysisOperatorError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GaussAnalysisOperatorError("science manifest is invalid") from error
    if value != science_manifest():
        raise GaussAnalysisOperatorError(
            "science manifest differs from the checked Gauss approval"
        )
    return cast(dict[str, object], value)


def load_capture_batch_snapshot(path: Path) -> CaptureBatchSnapshot:
    """Load one explicit canonical public snapshot without consulting capture state."""

    try:
        payload = path.read_bytes()
        if len(payload) > MAX_BATCH_SNAPSHOT_BYTES:
            raise GaussAnalysisOperatorError("batch snapshot exceeds the size limit")
        return decode_capture_batch_snapshot(payload)
    except GaussAnalysisOperatorError:
        raise
    except (OSError, ValueError) as error:
        raise GaussAnalysisOperatorError("batch snapshot is invalid") from error


def _batch_selection() -> ClosedBatchAnalysisSelection:
    return ClosedBatchAnalysisSelection(
        RECORDING_ALGORITHM_REF,
        RECORDING_CONFIG_REF,
        RECORDING_DEPENDENCY_REFS,
        SchemaRef(FeatureSetBundle.SCHEMA_ID),
    )


def _load_analysis_config(path: Path) -> AnalysisServiceConfig:
    config = load_service_config(path)
    if not isinstance(config, AnalysisServiceConfig):
        raise GaussAnalysisOperatorError("Gauss operator requires analysis config")
    if (
        config.job_repository_ref != JOB_REPOSITORY_REF
        or config.recording_reader_ref != RECORDING_READER_REF
        or config.feature_publisher_ref != FEATURE_PUBLISHER_REF
        or config.model_publisher_ref != MODEL_PUBLISHER_REF
        or config.runtime.secret_refs != (DATABASE_SECRET,)
    ):
        raise GaussAnalysisOperatorError(
            "analysis config differs from the checked Gauss capabilities"
        )
    return config


def _positive_bound(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("bound must be an integer") from error
    if result <= 0:
        raise argparse.ArgumentTypeError("bound must be positive")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leo-gauss-analysis",
        description="Submit or process exact post-capture analysis work on Gauss.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser(
        "validate",
        help=(
            "validate checked science and service config without credentials, "
            "database, CAS, or radio I/O"
        ),
    )
    validate.add_argument("--config", type=Path, required=True)
    validate.add_argument("--science-manifest", type=Path, required=True)

    submit = subparsers.add_parser(
        "submit", help="enqueue one exact published recording after capture"
    )
    submit.add_argument("--recording-id", required=True)
    submit.add_argument("--science-manifest", type=Path, required=True)
    submit.add_argument("--credential-directory", type=Path, required=True)

    submit_waterfall = subparsers.add_parser(
        "submit-waterfall",
        help="enqueue one exact post-capture waterfall for a published recording",
    )
    submit_waterfall.add_argument("--recording-id", required=True)
    submit_waterfall.add_argument("--science-manifest", type=Path, required=True)
    submit_waterfall.add_argument("--credential-directory", type=Path, required=True)
    submit_starlink = subparsers.add_parser(
        "submit-starlink",
        help=(
            "enqueue Qin known-code candidates for one exact 2.5 or 5 MS/s "
            "edge-scan recording"
        ),
    )
    submit_starlink.add_argument("--recording-id", required=True)
    submit_starlink.add_argument("--science-manifest", type=Path, required=True)
    submit_starlink.add_argument("--credential-directory", type=Path, required=True)
    submit_starlink_suite = subparsers.add_parser(
        "submit-starlink-suite",
        help="enqueue the detector suite and additive acquired-QAM dwell product",
    )
    submit_starlink_suite.add_argument("--recording-id", required=True)
    submit_starlink_suite.add_argument("--science-manifest", type=Path, required=True)
    submit_starlink_suite.add_argument(
        "--credential-directory", type=Path, required=True
    )
    backfill_acquired_qam = subparsers.add_parser(
        "backfill-acquired-qam-v0-3",
        help="publish only additive V17 QAM evidence from an existing source suite",
    )
    backfill_acquired_qam.add_argument("--recording-id", required=True)
    backfill_acquired_qam.add_argument("--science-manifest", type=Path, required=True)
    backfill_acquired_qam.add_argument(
        "--credential-directory", type=Path, required=True
    )

    submit_batch = subparsers.add_parser(
        "submit-batch",
        help="verify one explicit terminal public batch and enqueue its recordings",
    )
    submit_batch.add_argument("--batch-snapshot", type=Path, required=True)
    submit_batch.add_argument("--science-manifest", type=Path, required=True)
    submit_batch.add_argument("--credential-directory", type=Path, required=True)

    process = subparsers.add_parser(
        "process-one", help="claim and process at most one durable analysis job"
    )
    process.add_argument("--config", type=Path, required=True)
    process.add_argument("--science-manifest", type=Path, required=True)
    process.add_argument("--credential-directory", type=Path, required=True)
    process_waterfall = subparsers.add_parser(
        "process-waterfall-one",
        help="claim and process at most one durable waterfall job",
    )
    process_waterfall.add_argument("--science-manifest", type=Path, required=True)
    process_waterfall.add_argument("--credential-directory", type=Path, required=True)
    project_waterfall = subparsers.add_parser(
        "project-waterfall-one",
        help="project at most one durable waterfall result to the dashboard",
    )
    project_waterfall.add_argument("--science-manifest", type=Path, required=True)
    project_waterfall.add_argument("--credential-directory", type=Path, required=True)
    process_starlink = subparsers.add_parser(
        "process-starlink-one",
        help="claim and process at most one durable Starlink candidate job",
    )
    process_starlink.add_argument("--science-manifest", type=Path, required=True)
    process_starlink.add_argument("--credential-directory", type=Path, required=True)
    project_starlink = subparsers.add_parser(
        "project-starlink-one",
        help="project at most one candidate-only Starlink result to the dashboard",
    )
    project_starlink.add_argument("--science-manifest", type=Path, required=True)
    project_starlink.add_argument("--credential-directory", type=Path, required=True)
    process_starlink_suite = subparsers.add_parser(
        "process-starlink-suite-one",
        help="claim and process at most one detector-suite/acquired-QAM job",
    )
    process_starlink_suite.add_argument("--science-manifest", type=Path, required=True)
    process_starlink_suite.add_argument(
        "--credential-directory", type=Path, required=True
    )
    project_starlink_suite = subparsers.add_parser(
        "project-starlink-suite-one",
        help="project at most one durable detector-suite result to the dashboard",
    )
    project_starlink_suite.add_argument("--science-manifest", type=Path, required=True)
    project_starlink_suite.add_argument(
        "--credential-directory", type=Path, required=True
    )
    project = subparsers.add_parser(
        "project-one",
        help="project at most one durable FeatureSet work item to the dashboard",
    )
    project.add_argument("--science-manifest", type=Path, required=True)
    project.add_argument("--credential-directory", type=Path, required=True)

    drain = subparsers.add_parser(
        "drain-batch",
        help="under one mode lock, submit and bounded-drain analysis and projection",
    )
    drain.add_argument("--batch-snapshot", type=Path, required=True)
    drain.add_argument("--config", type=Path, required=True)
    drain.add_argument("--science-manifest", type=Path, required=True)
    drain.add_argument("--credential-directory", type=Path, required=True)
    drain.add_argument("--max-analysis-jobs", type=_positive_bound, required=True)
    drain.add_argument("--max-projection-work", type=_positive_bound, required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    submitter: Submitter = _submit,
    batch_submitter: BatchSubmitter = _submit_closed_batch,
    processor: Processor = _process_one,
    projector: Projector = _project_one,
    waterfall_submitter: WaterfallSubmitter = _submit_waterfall,
    waterfall_processor: WaterfallProcessor = _process_waterfall_one,
    waterfall_projector: WaterfallProjector = _project_waterfall_one,
    starlink_submitter: StarlinkSubmitter = _submit_starlink,
    starlink_processor: StarlinkProcessor = _process_starlink_one,
    starlink_projector: StarlinkProjector = _project_starlink_one,
    starlink_suite_submitter: StarlinkSuiteSubmitter = _submit_starlink_suite,
    starlink_suite_processor: StarlinkSuiteProcessor = _process_starlink_suite_one,
    starlink_suite_projector: StarlinkSuiteProjector = _project_starlink_suite_one,
    acquired_qam_backfiller: AcquiredQamBackfiller = (
        _backfill_starlink_acquired_qam_v0_3
    ),
    mode_lock_factory: ModeLockFactory = ExclusiveModeLock,
    credential_factory: CredentialFactory = SystemdCredentialProvider,
) -> int:
    arguments = _parser().parse_args(argv)
    snapshot: CaptureBatchSnapshot | None = None
    try:
        load_approved_manifest(arguments.science_manifest)
        config = (
            _load_analysis_config(arguments.config)
            if arguments.command in {"validate", "process-one", "drain-batch"}
            else None
        )
        if arguments.command in {"submit-batch", "drain-batch"}:
            snapshot = load_capture_batch_snapshot(arguments.batch_snapshot)
        if arguments.command in {
            "validate",
            "process-one",
            "project-one",
            "process-waterfall-one",
            "project-waterfall-one",
            "submit-starlink",
            "process-starlink-one",
            "project-starlink-one",
            "submit-starlink-suite",
            "process-starlink-suite-one",
            "project-starlink-suite-one",
            "backfill-acquired-qam-v0-3",
            "drain-batch",
        }:
            require_approved_runtime()
    except (
        ConfigurationError,
        GaussAnalysisOperatorError,
        GaussRuntimeApprovalError,
        ValueError,
    ):
        _emit(stderr, {"event": "gauss_analysis_configuration_error"})
        return ExitCode.USAGE_OR_CONFIG

    if arguments.command == "validate":
        assert config is not None
        _emit(
            stdout,
            {
                "event": "gauss_analysis_valid",
                "instance_id": config.runtime.instance_id,
                "science_manifest_digest": str(SCIENCE_MANIFEST_DIGEST),
                "source_commit": science_manifest()["source_commit"],
            },
        )
        return ExitCode.OK

    if arguments.command == "submit":
        try:
            submitted = _under_mode_lock(
                mode_lock_factory,
                lambda: submitter(
                    RecordingSubmissionOperatorConfig(
                        ExactRecordingAnalysisSelection(
                            RecordingId(arguments.recording_id),
                            RECORDING_ALGORITHM_REF,
                            RECORDING_CONFIG_REF,
                            RECORDING_DEPENDENCY_REFS,
                            SchemaRef(FeatureSetBundle.SCHEMA_ID),
                        ),
                        "catalog-dsn",
                    ),
                    credential_factory(arguments.credential_directory),
                ),
            )
        except Exception:  # noqa: BLE001 - sanitize the process boundary
            _emit(stderr, {"event": "gauss_analysis_submission_failed"})
            return ExitCode.SUBMISSION_FAILED
        _emit(
            stdout,
            {
                "event": "gauss_analysis_submitted",
                "job_id": str(submitted.job_id),
                "recording_id": str(submitted.request.recording_id),
                "science_manifest_digest": str(SCIENCE_MANIFEST_DIGEST),
            },
        )
        return ExitCode.OK

    if arguments.command == "submit-waterfall":
        try:
            submitted_waterfall = _under_mode_lock(
                mode_lock_factory,
                lambda: waterfall_submitter(
                    RecordingId(arguments.recording_id),
                    credential_factory(arguments.credential_directory),
                ),
            )
        except Exception:  # noqa: BLE001 - sanitize the process boundary
            _emit(stderr, {"event": "gauss_waterfall_submission_failed"})
            return ExitCode.SUBMISSION_FAILED
        _emit(
            stdout,
            {
                "event": "gauss_waterfall_submitted",
                "job_id": str(submitted_waterfall.job_id),
                "recording_id": str(submitted_waterfall.request.recording_id),
                "science_manifest_digest": str(SCIENCE_MANIFEST_DIGEST),
            },
        )
        return ExitCode.OK

    if arguments.command == "submit-starlink":
        try:
            submitted_starlink = _under_mode_lock(
                mode_lock_factory,
                lambda: starlink_submitter(
                    RecordingId(arguments.recording_id),
                    credential_factory(arguments.credential_directory),
                ),
            )
        except Exception:  # noqa: BLE001 - sanitize the process boundary
            _emit(stderr, {"event": "gauss_starlink_submission_failed"})
            return ExitCode.SUBMISSION_FAILED
        _emit(
            stdout,
            {
                "event": "gauss_starlink_submitted",
                "job_id": str(submitted_starlink.job_id),
                "recording_id": str(submitted_starlink.request.recording_id),
                "candidate_semantics": "uncalibrated-search-only",
                "science_manifest_digest": str(SCIENCE_MANIFEST_DIGEST),
            },
        )
        return ExitCode.OK

    if arguments.command == "submit-starlink-suite":
        try:
            submitted_suite = _under_mode_lock(
                mode_lock_factory,
                lambda: starlink_suite_submitter(
                    RecordingId(arguments.recording_id),
                    credential_factory(arguments.credential_directory),
                ),
            )
        except Exception:  # noqa: BLE001 - sanitize the process boundary
            _emit(stderr, {"event": "gauss_starlink_suite_submission_failed"})
            return ExitCode.SUBMISSION_FAILED
        _emit(
            stdout,
            {
                "event": "gauss_starlink_suite_submitted",
                "job_id": str(submitted_suite.job_id),
                "recording_id": str(submitted_suite.request.recording_id),
                "science_manifest_digest": str(SCIENCE_MANIFEST_DIGEST),
            },
        )
        return ExitCode.OK

    if arguments.command == "backfill-acquired-qam-v0-3":
        try:
            # This command reads an immutable recording/source-suite pair and
            # publishes only an additive V0.3 product.  It neither contacts a
            # radio nor changes capture mode, so it must remain runnable while
            # continuous capture owns the pipeline-mode lock.
            acquired_qam_backfiller(
                RecordingId(arguments.recording_id),
                credential_factory(arguments.credential_directory),
            )
        except Exception:  # noqa: BLE001 - sanitize the process boundary
            _emit(stderr, {"event": "gauss_acquired_qam_v0_3_backfill_failed"})
            return ExitCode.ANALYSIS_FAILED
        _emit(
            stdout,
            {
                "event": "gauss_acquired_qam_v0_3_backfill_complete",
                "recording_id": arguments.recording_id,
                "science_manifest_digest": str(SCIENCE_MANIFEST_DIGEST),
            },
        )
        return ExitCode.OK

    if arguments.command == "submit-batch":
        assert snapshot is not None
        try:
            submitted_batch = _under_mode_lock(
                mode_lock_factory,
                lambda: batch_submitter(
                    snapshot,
                    _batch_selection(),
                    credential_factory(arguments.credential_directory),
                ),
            )
        except Exception:  # noqa: BLE001 - sanitize the process boundary
            _emit(stderr, {"event": "gauss_batch_submission_failed"})
            return ExitCode.SUBMISSION_FAILED
        _emit(
            stdout, _submitted_batch_payload(submitted_batch, "gauss_batch_submitted")
        )
        return ExitCode.OK

    if arguments.command == "project-one":
        try:
            progressed = _under_mode_lock(
                mode_lock_factory,
                lambda: projector(credential_factory(arguments.credential_directory)),
            )
        except Exception:  # noqa: BLE001 - sanitize the process boundary
            _emit(stderr, {"event": "gauss_feature_projection_failed"})
            return ExitCode.ANALYSIS_FAILED
        _emit(
            stdout,
            {
                "event": "gauss_feature_projection_cycle_complete",
                "forward_progress": progressed,
                "science_manifest_digest": str(SCIENCE_MANIFEST_DIGEST),
            },
        )
        return ExitCode.OK

    if arguments.command == "process-waterfall-one":
        try:
            waterfall_progress = _under_mode_lock(
                mode_lock_factory,
                lambda: waterfall_processor(
                    credential_factory(arguments.credential_directory)
                ),
            )
        except Exception:  # noqa: BLE001 - sanitize the process boundary
            _emit(stderr, {"event": "gauss_waterfall_analysis_failed"})
            return ExitCode.ANALYSIS_FAILED
        _emit(
            stdout,
            {
                "event": "gauss_waterfall_analysis_cycle_complete",
                "forward_progress": waterfall_progress,
                "science_manifest_digest": str(SCIENCE_MANIFEST_DIGEST),
            },
        )
        return ExitCode.OK

    if arguments.command == "project-waterfall-one":
        try:
            waterfall_projection_progress = _under_mode_lock(
                mode_lock_factory,
                lambda: waterfall_projector(
                    credential_factory(arguments.credential_directory)
                ),
            )
        except Exception:  # noqa: BLE001 - sanitize the process boundary
            _emit(stderr, {"event": "gauss_waterfall_projection_failed"})
            return ExitCode.ANALYSIS_FAILED
        _emit(
            stdout,
            {
                "event": "gauss_waterfall_projection_cycle_complete",
                "forward_progress": waterfall_projection_progress,
                "science_manifest_digest": str(SCIENCE_MANIFEST_DIGEST),
            },
        )
        return ExitCode.OK

    if arguments.command == "process-starlink-one":
        try:
            starlink_progress = _under_mode_lock(
                mode_lock_factory,
                lambda: starlink_processor(
                    credential_factory(arguments.credential_directory)
                ),
            )
        except Exception:  # noqa: BLE001 - sanitize the process boundary
            _emit(stderr, {"event": "gauss_starlink_analysis_failed"})
            return ExitCode.ANALYSIS_FAILED
        _emit(
            stdout,
            {
                "event": "gauss_starlink_analysis_cycle_complete",
                "forward_progress": starlink_progress,
                "candidate_semantics": "uncalibrated-search-only",
                "science_manifest_digest": str(SCIENCE_MANIFEST_DIGEST),
            },
        )
        return ExitCode.OK

    if arguments.command == "process-starlink-suite-one":
        try:
            suite_progress = _under_mode_lock(
                mode_lock_factory,
                lambda: starlink_suite_processor(
                    credential_factory(arguments.credential_directory)
                ),
            )
        except Exception:  # noqa: BLE001 - sanitize the process boundary
            _emit(stderr, {"event": "gauss_starlink_suite_analysis_failed"})
            return ExitCode.ANALYSIS_FAILED
        _emit(
            stdout,
            {
                "event": "gauss_starlink_suite_analysis_cycle_complete",
                "forward_progress": suite_progress,
                "science_manifest_digest": str(SCIENCE_MANIFEST_DIGEST),
            },
        )
        return ExitCode.OK

    if arguments.command == "project-starlink-one":
        try:
            starlink_projection_progress = _under_mode_lock(
                mode_lock_factory,
                lambda: starlink_projector(
                    credential_factory(arguments.credential_directory)
                ),
            )
        except Exception:  # noqa: BLE001 - sanitize the process boundary
            _emit(stderr, {"event": "gauss_starlink_projection_failed"})
            return ExitCode.ANALYSIS_FAILED
        _emit(
            stdout,
            {
                "event": "gauss_starlink_projection_cycle_complete",
                "forward_progress": starlink_projection_progress,
                "candidate_semantics": "uncalibrated-search-only",
                "science_manifest_digest": str(SCIENCE_MANIFEST_DIGEST),
            },
        )
        return ExitCode.OK

    if arguments.command == "project-starlink-suite-one":
        try:
            suite_projection_progress = _under_mode_lock(
                mode_lock_factory,
                lambda: starlink_suite_projector(
                    credential_factory(arguments.credential_directory)
                ),
            )
        except Exception:  # noqa: BLE001 - sanitize the process boundary
            _emit(stderr, {"event": "gauss_starlink_suite_projection_failed"})
            return ExitCode.ANALYSIS_FAILED
        _emit(
            stdout,
            {
                "event": "gauss_starlink_suite_projection_cycle_complete",
                "forward_progress": suite_projection_progress,
                "science_manifest_digest": str(SCIENCE_MANIFEST_DIGEST),
            },
        )
        return ExitCode.OK

    if arguments.command == "drain-batch":
        assert config is not None
        assert snapshot is not None
        try:
            result = _under_mode_lock(
                mode_lock_factory,
                lambda: _drain_batch(
                    snapshot,
                    config,
                    credential_factory(arguments.credential_directory),
                    stdout,
                    batch_submitter=batch_submitter,
                    processor=processor,
                    projector=projector,
                    max_analysis_jobs=arguments.max_analysis_jobs,
                    max_projection_work=arguments.max_projection_work,
                ),
            )
        except Exception:  # noqa: BLE001 - sanitize the process boundary
            _emit(stderr, {"event": "gauss_batch_drain_failed"})
            return ExitCode.ANALYSIS_FAILED
        _emit(stdout, result)
        return ExitCode.OK

    assert config is not None
    try:
        progressed = _under_mode_lock(
            mode_lock_factory,
            lambda: processor(
                config,
                credential_factory(arguments.credential_directory),
                stdout,
            ),
        )
    except Exception:  # noqa: BLE001 - sanitize the process boundary
        _emit(stderr, {"event": "gauss_analysis_cycle_failed"})
        return ExitCode.ANALYSIS_FAILED
    _emit(
        stdout,
        {
            "event": "gauss_analysis_cycle_complete",
            "forward_progress": progressed,
            "science_manifest_digest": str(SCIENCE_MANIFEST_DIGEST),
        },
    )
    return ExitCode.OK


def _under_mode_lock(
    factory: ModeLockFactory,
    operation: Callable[[], Any],
) -> Any:
    lock = factory(MODE_LOCK_PATH)
    lock.acquire()
    try:
        return operation()
    finally:
        lock.release()


def _drain_batch(
    snapshot: CaptureBatchSnapshot,
    config: AnalysisServiceConfig,
    credentials: SecretProvider,
    stdout: TextIO,
    *,
    batch_submitter: BatchSubmitter,
    processor: Processor,
    projector: Projector,
    max_analysis_jobs: int,
    max_projection_work: int,
) -> dict[str, object]:
    submitted = batch_submitter(snapshot, _batch_selection(), credentials)
    analysis_processed, analysis_no_claimable = _bounded_drain(
        max_analysis_jobs,
        lambda: processor(config, credentials, stdout),
    )
    projections_processed, projection_no_claimable = _bounded_drain(
        max_projection_work,
        lambda: projector(credentials),
    )
    payload = _submitted_batch_payload(submitted, "gauss_batch_bounded_cycle")
    payload.update(
        {
            "analysis_processed": analysis_processed,
            "analysis_no_claimable_work": analysis_no_claimable,
            "feature_projections_processed": projections_processed,
            "feature_projection_no_claimable_work": projection_no_claimable,
            "max_analysis_jobs": max_analysis_jobs,
            "max_projection_work": max_projection_work,
        }
    )
    return payload


def _bounded_drain(bound: int, process_one: Callable[[], bool]) -> tuple[int, bool]:
    processed = 0
    for _ in range(bound):
        if not process_one():
            return processed, True
        processed += 1
    return processed, False


def _submitted_batch_payload(
    submitted: SubmittedClosedBatchAnalysis,
    event: str,
) -> dict[str, object]:
    return {
        "event": event,
        "batch_id": str(submitted.snapshot.batch_id),
        "paired_analysis_eligibility": (submitted.paired_analysis_eligibility.value),
        "paired_science_submitted": False,
        "recording_jobs": [
            {
                "recording_id": str(item.request.recording_id),
                "job_id": str(item.job_id),
            }
            for item in submitted.recording_jobs
        ],
        "science_manifest_digest": str(SCIENCE_MANIFEST_DIGEST),
    }


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _bad(f"duplicate science manifest key: {key}")
        result[key] = value
    return result


def _bad(message: str) -> NoReturn:
    raise GaussAnalysisOperatorError(message)


def _emit(stream: TextIO, payload: dict[str, object]) -> None:
    stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    stream.flush()


if __name__ == "__main__":  # pragma: no cover - exercised through main
    raise SystemExit(main())
