"""Offline two-stage analysis composition with exact scientific plugins.

This module performs no I/O at import time and intentionally exports no global
production ``DeploymentPlugin``. :func:`build_station_plugin` makes the complete
infrastructure plugin only after a station injects the exact, versioned
scientific implementations it has approved.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, BinaryIO, Protocol, cast

from leo_flow.analysis.dataset import DatasetSnapshotReader
from leo_flow.analysis.ephemeris.catalog import EphemerisSnapshotCatalog
from leo_flow.contracts.core import ArtifactRef, Digest
from leo_flow.contracts.ephemeris import EphemerisSnapshotRef
from leo_flow.contracts.features import FeatureSetBundle, RecordingAnalysisRequest
from leo_flow.contracts.model import (
    FeatureDatasetSnapshot,
    ModelAnalysisRequest,
    ModelSnapshotBundle,
)
from leo_flow.contracts.ports import (
    EphemerisReader,
    FeatureSetReader,
    HardwareMetadataReader,
    ModelFitter,
    RecordingAnalyzer,
)
from leo_flow.contracts.storage import ByteRange, ObjectMetadata, ObjectRef
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.jobs.ports import JobLeaseRepository, StaleLeaseError
from leo_flow.services.analysis import build_analysis_service
from leo_flow.services.analysis_router import (
    AnalysisLeaseExecutor,
    TypedAnalysisRouterCycle,
)
from leo_flow.services.bootstrap import (
    AdapterBuildContext,
    AdapterManifest,
    AdapterSet,
    Capability,
    DeploymentPlugin,
    Process,
)
from leo_flow.services.config import AnalysisServiceConfig, SecretRef, ServiceConfig
from leo_flow.services.lifecycle import DiagnosticSink, ServiceLoop
from leo_flow.services.model_analysis import (
    ModelAnalysisCommitter,
    ModelAnalysisJobPreparer,
    ModelAnalysisJobProcessor,
    ModelFitterFactory,
)
from leo_flow.services.recording_analysis import (
    FencedRecordingAnalysisWorker,
    RecordingAnalysisCommitter,
    RecordingAnalysisJobPreparer,
)
from leo_flow.storage.ports import (
    BlobReader,
    BlobWriter,
    RecordingObjectReader,
    RecordingView,
)

if TYPE_CHECKING:
    import psycopg


JOB_REPOSITORY_REF = "jobs.postgres-v1"
RECORDING_READER_REF = "recordings.sigmf-cas-v1"
FEATURE_PUBLISHER_REF = "features.atomic-postgres-cas-v1"
MODEL_PUBLISHER_REF = "models.atomic-postgres-cas-v1"
SECRET_PROVIDER = "systemd-credential"
DATABASE_SECRET = SecretRef(SECRET_PROVIDER, "catalog-dsn")
POSTGRES_TIMEOUT_S = 5
MAX_NORMALIZED_EPHEMERIS_BYTES = 16 * 1024 * 1024
EXPECTED_MIGRATIONS = tuple(
    f"{number:04d}_{name}.sql"
    for number, name in (
        (1, "first_slice"),
        (2, "capability_roles"),
        (3, "ephemeris_catalog"),
        (4, "dashboard_projections"),
        (5, "dataset_snapshots"),
        (6, "dashboard_projection_identity"),
        (7, "feature_set_catalog"),
        (8, "model_snapshot_catalog"),
        (9, "recording_ephemeris_link"),
        (10, "hardware_metadata_catalog"),
        (11, "recording_hardware_link"),
        (12, "detector_evaluation_catalog"),
        (13, "object_retention_gc"),
        (14, "unregistered_object_reconciliation"),
        (15, "job_parking"),
        (16, "tracking_input_catalog"),
        (17, "security_definer_hardening"),
        (18, "tracking_model_snapshot_catalog"),
        (19, "dwell_request_ingress"),
        (20, "feature_projection_work"),
        (21, "dashboard_capture_batch_projection"),
        (22, "analysis_migration_receipt_read"),
        (23, "campaign_projection_receipt"),
        (24, "capture_analysis_inactive"),
        (25, "recording_waterfall_analysis"),
        (26, "dashboard_recording_detail_waterfall_projection"),
        (27, "capture_analysis_waterfall_drain"),
        (28, "recording_starlink_candidate_pipeline"),
        (29, "starlink_detector_suite_v0_2"),
        (30, "campaign_scoped_analysis_claims"),
        (31, "radio_lifecycle_detection"),
        (32, "campaign_online_analysis"),
        (33, "registered_analysis_during_capture"),
        (34, "waterfall_v0_2_doppler_analysis"),
        (35, "starlink_surrogate_null_catalog"),
        (36, "starlink_pilot_constellation_catalog"),
        (37, "focused_analysis_during_capture"),
    )
)


class OfflineAnalysisCompositionError(RuntimeError):
    """The selected offline job or scientific implementation is unavailable."""


@dataclass(frozen=True)
class AlgorithmKey:
    """Exact immutable algorithm/config identity used for plugin selection."""

    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef


class ExactRecordingAnalyzerRegistry:
    """Dispatch one recording to an explicitly registered exact implementation."""

    def __init__(self, analyzers: Mapping[AlgorithmKey, RecordingAnalyzer]) -> None:
        if not analyzers:
            raise ValueError("at least one recording analyzer is required")
        self._analyzers = MappingProxyType(dict(analyzers))

    def analyze(
        self, recording: RecordingView, request: RecordingAnalysisRequest
    ) -> FeatureSetBundle:
        try:
            analyzer = self._analyzers[
                AlgorithmKey(request.algorithm_ref, request.config_ref)
            ]
        except KeyError as error:
            raise OfflineAnalysisCompositionError(
                "exact recording algorithm and configuration are not registered"
            ) from error
        return analyzer.analyze(recording, request)


ModelFitterBuilder = Callable[[FeatureDatasetSnapshot], ModelFitter]


class ExactModelFitterRegistry:
    """Build a request-checking fitter over an immutable exact registry."""

    def __init__(self, fitters: Mapping[AlgorithmKey, ModelFitterBuilder]) -> None:
        if not fitters:
            raise ValueError("at least one model fitter is required")
        self._fitters = MappingProxyType(dict(fitters))

    def __call__(self, dataset: FeatureDatasetSnapshot) -> ModelFitter:
        return _RequestSelectedFitter(dataset, self._fitters)


@dataclass(frozen=True)
class StationScientificFactories:
    """Complete, exact scientific registry supplied by a station package.

    Constructing this value is the approval boundary.  This deployment never
    discovers algorithms from files, entry points, database rows, or aliases.
    """

    recording_analyzers: Mapping[AlgorithmKey, RecordingAnalyzer]
    model_fitters: Mapping[AlgorithmKey, ModelFitterBuilder]

    def __post_init__(self) -> None:
        if not self.recording_analyzers or not self.model_fitters:
            raise ValueError("both exact scientific registries are required")
        object.__setattr__(
            self,
            "recording_analyzers",
            MappingProxyType(dict(self.recording_analyzers)),
        )
        object.__setattr__(
            self, "model_fitters", MappingProxyType(dict(self.model_fitters))
        )


@dataclass(frozen=True)
class _JobRepositorySpec:
    dsn: str


@dataclass(frozen=True)
class _RecordingReaderSpec:
    cas_root: Path


@dataclass(frozen=True)
class _FeatureLaneSpec:
    dsn: str
    analyzers: Mapping[AlgorithmKey, RecordingAnalyzer]


@dataclass(frozen=True)
class _ModelLaneSpec:
    dsn: str
    fitters: Mapping[AlgorithmKey, ModelFitterBuilder]


@dataclass(frozen=True)
class _RequestSelectedFitter:
    dataset: FeatureDatasetSnapshot
    fitters: Mapping[AlgorithmKey, ModelFitterBuilder]

    def fit(
        self,
        request: ModelAnalysisRequest,
        features: FeatureSetReader,
        ephemerides: EphemerisReader,
        hardware: HardwareMetadataReader,
    ) -> ModelSnapshotBundle:
        key = AlgorithmKey(request.algorithm_ref, request.model_config_ref)
        try:
            fitter = self.fitters[key](self.dataset)
        except KeyError as error:
            raise OfflineAnalysisCompositionError(
                "exact model algorithm and configuration are not registered"
            ) from error
        return fitter.fit(request, features, ephemerides, hardware)


@dataclass(frozen=True)
class OfflineAnalysisComponents:
    """Infrastructure and scientific seams required by the offline process."""

    jobs: JobLeaseRepository
    recordings: RecordingObjectReader
    recording_analyzer: RecordingAnalyzer
    recording_committer: RecordingAnalysisCommitter
    datasets: DatasetSnapshotReader
    features: FeatureSetReader
    ephemerides: EphemerisReader
    hardware: HardwareMetadataReader
    model_fitter_factory: ModelFitterFactory
    model_committer: ModelAnalysisCommitter
    preflight: Callable[[], None] = lambda: None
    close: Callable[[float], None] = lambda timeout_s: None


class FencedModelAnalysisExecutor:
    """Give preparation failures the same explicit fenced state as recording jobs."""

    def __init__(
        self, jobs: JobLeaseRepository, processor: AnalysisLeaseExecutor
    ) -> None:
        self._jobs = jobs
        self._processor = processor

    def execute(self, lease: JobLease) -> object:
        if lease.job_type is not JobType.MODEL_ANALYSIS:
            raise OfflineAnalysisCompositionError(
                "model executor received a different job type"
            )
        try:
            return self._processor.execute(lease)
        except Exception as error:
            try:
                self._jobs.fail(
                    lease.job_id,
                    lease.lease_token,
                    lease.lease_generation,
                    f"{type(error).__name__}: model analysis failed",
                    None,
                )
            except StaleLeaseError:
                pass
            raise


class OfflineAnalysisCycle(TypedAnalysisRouterCycle):
    """Compatibility constructor for the shared typed analysis router."""

    CLAIMED_TYPES = (JobType.RECORDING_ANALYSIS, JobType.MODEL_ANALYSIS)

    def __init__(
        self,
        jobs: JobLeaseRepository,
        *,
        recording: AnalysisLeaseExecutor,
        model: AnalysisLeaseExecutor,
        worker_id: str,
        lease_ttl_s: float,
        preflight: Callable[[], None] = lambda: None,
        close: Callable[[float], None] = lambda timeout_s: None,
    ) -> None:
        super().__init__(
            jobs,
            executors={
                JobType.RECORDING_ANALYSIS: recording,
                JobType.MODEL_ANALYSIS: model,
            },
            worker_id=worker_id,
            lease_ttl_s=lease_ttl_s,
            preflight=preflight,
            close=close,
        )


def build_offline_analysis_cycle(
    config: AnalysisServiceConfig,
    components: OfflineAnalysisComponents,
    *,
    lease_ttl_s: float,
) -> OfflineAnalysisCycle:
    """Compose durable executors without importing capture or provider clients."""

    recording = FencedRecordingAnalysisWorker(
        components.jobs,
        RecordingAnalysisJobPreparer(
            components.recordings, components.recording_analyzer
        ),
        components.recording_committer,
        worker_id=config.runtime.instance_id,
        lease_ttl_s=lease_ttl_s,
    )
    model = FencedModelAnalysisExecutor(
        components.jobs,
        ModelAnalysisJobProcessor(
            ModelAnalysisJobPreparer(
                components.datasets,
                components.features,
                components.ephemerides,
                components.hardware,
                components.model_fitter_factory,
            ),
            components.model_committer,
        ),
    )
    return OfflineAnalysisCycle(
        components.jobs,
        recording=recording,
        model=model,
        worker_id=config.runtime.instance_id,
        lease_ttl_s=lease_ttl_s,
        preflight=components.preflight,
        close=components.close,
    )


def build_offline_analysis_service(
    config: AnalysisServiceConfig,
    components: OfflineAnalysisComponents,
    *,
    lease_ttl_s: float,
    diagnostics: DiagnosticSink | None = None,
) -> ServiceLoop:
    """Build the runnable lifecycle around the strict two-stage cycle."""

    return build_analysis_service(
        config,
        build_offline_analysis_cycle(config, components, lease_ttl_s=lease_ttl_s),
        diagnostics=diagnostics,
    )


class _DeferredFileSystemBlobStore:
    """Keep filesystem effects behind process preflight."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise ValueError("analysis CAS root must be absolute")
        self._root = root
        self._store: _BlobStore | None = None

    def preflight(self) -> None:
        from leo_flow.storage.filesystem import FileSystemBlobStore

        store = FileSystemBlobStore(self._root)
        probe: int | None = None
        probe_path: str | None = None
        try:
            probe, probe_path = tempfile.mkstemp(
                prefix="readiness-", suffix=".tmp", dir=self._root / ".tmp"
            )
            os.write(probe, b"analysis-cas-readiness-v1")
            os.fsync(probe)
        finally:
            if probe is not None:
                os.close(probe)
            if probe_path is not None:
                Path(probe_path).unlink(missing_ok=True)
        self._store = store

    def _ready(self) -> _BlobStore:
        if self._store is None:
            raise OfflineAnalysisCompositionError(
                "analysis CAS was used before successful preflight"
            )
        return self._store

    def put(
        self,
        stream: BinaryIO,
        *,
        expected_digest: Digest,
        expected_bytes: int,
        media_type: str,
        format_id: str,
        idempotency_key: str,
    ) -> ObjectRef:
        return self._ready().put(
            stream,
            expected_digest=expected_digest,
            expected_bytes=expected_bytes,
            media_type=media_type,
            format_id=format_id,
            idempotency_key=idempotency_key,
        )

    def head(self, ref: ObjectRef) -> ObjectMetadata:
        return self._ready().head(ref)

    def open(
        self, ref: ObjectRef, byte_range: ByteRange | None = None
    ) -> AbstractContextManager[BinaryIO]:
        return self._ready().open(ref, byte_range)


class _BlobStore(BlobReader, BlobWriter, Protocol):
    pass


@dataclass(frozen=True)
class _EphemerisView:
    ref: EphemerisSnapshotRef
    payload: bytes

    def normalized_bytes(self) -> bytes:
        return self.payload


class _ExactArchivedEphemerisReader:
    """Open normalized bytes only through an exact archived snapshot identity."""

    def __init__(self, catalog: EphemerisSnapshotCatalog, blobs: BlobReader) -> None:
        self._catalog = catalog
        self._blobs = blobs

    @contextmanager
    def open(self, ref: EphemerisSnapshotRef) -> Iterator[_EphemerisView]:
        archived = self._catalog.get(ref.snapshot_id)
        if archived is None or archived.snapshot_ref() != ref:
            raise OfflineAnalysisCompositionError(
                "no archived ephemeris exactly matches the requested reference"
            )
        normalized_ref = archived.snapshot.normalized_object_ref
        if (
            normalized_ref.digest != ref.normalized_digest
            or normalized_ref.media_type != "application/json"
            or normalized_ref.format_id != "tle-normalized-v1"
            or normalized_ref.byte_count > MAX_NORMALIZED_EPHEMERIS_BYTES
        ):
            raise OfflineAnalysisCompositionError(
                "ephemeris archive normalized object metadata differs"
            )
        metadata = self._blobs.head(normalized_ref)
        if metadata.ref != normalized_ref or not metadata.verified:
            raise OfflineAnalysisCompositionError(
                "ephemeris normalized object was not verified"
            )
        with self._blobs.open(normalized_ref) as stream:
            payload = stream.read(normalized_ref.byte_count + 1)
        if (
            len(payload) != normalized_ref.byte_count
            or Digest.sha256(payload) != normalized_ref.digest
        ):
            raise OfflineAnalysisCompositionError(
                "ephemeris normalized bytes differ from the archive"
            )
        yield _EphemerisView(ref, payload)


def _dsn(context: AdapterBuildContext) -> str:
    try:
        return context.secrets[DATABASE_SECRET]
    except KeyError as error:
        raise ValueError("catalog database credential was not configured") from error


def _job_spec(context: AdapterBuildContext) -> _JobRepositorySpec:
    return _JobRepositorySpec(_dsn(context))


def _recording_spec(cas_root: Path) -> Callable[[AdapterBuildContext], object]:
    def build(context: AdapterBuildContext) -> object:
        del context
        return _RecordingReaderSpec(cas_root)

    return build


def _feature_spec(
    analyzers: Mapping[AlgorithmKey, RecordingAnalyzer],
) -> Callable[[AdapterBuildContext], object]:
    def build(context: AdapterBuildContext) -> object:
        return _FeatureLaneSpec(_dsn(context), analyzers)

    return build


def _model_spec(
    fitters: Mapping[AlgorithmKey, ModelFitterBuilder],
) -> Callable[[AdapterBuildContext], object]:
    def build(context: AdapterBuildContext) -> object:
        return _ModelLaneSpec(_dsn(context), fitters)

    return build


def _build_postgres_components(
    config: ServiceConfig,
    adapters: AdapterSet,
    diagnostics: DiagnosticSink,
    *,
    lease_ttl_s: float,
) -> ServiceLoop:
    if not isinstance(config, AnalysisServiceConfig):
        raise TypeError("offline analysis v1 requires analysis configuration")
    job_spec = cast(_JobRepositorySpec, adapters[Capability.JOB_REPOSITORY])
    recording_spec = cast(_RecordingReaderSpec, adapters[Capability.RECORDING_READER])
    feature_spec = cast(_FeatureLaneSpec, adapters[Capability.FEATURE_PUBLISHER])
    model_spec = cast(_ModelLaneSpec, adapters[Capability.MODEL_PUBLISHER])
    if len({job_spec.dsn, feature_spec.dsn, model_spec.dsn}) != 1:
        raise OfflineAnalysisCompositionError(
            "analysis capabilities do not share one catalog identity"
        )

    try:
        import psycopg
        from psycopg.rows import dict_row

        from leo_flow.adapters.feature_postgres_catalog import PostgresFeatureSetCatalog
        from leo_flow.adapters.hardware_postgres_catalog import (
            PostgresHardwareSnapshotCatalog,
        )
        from leo_flow.adapters.model_analysis_postgres import (
            AtomicPostgresModelAnalysisCommitter,
        )
        from leo_flow.adapters.recording_analysis_postgres import (
            AtomicPostgresRecordingAnalysisCommitter,
        )
        from leo_flow.analysis.dataset import DurableDatasetSnapshotRepository
        from leo_flow.analysis.dataset.postgres_catalog import (
            PostgresDatasetSnapshotCatalog,
        )
        from leo_flow.analysis.ephemeris.postgres_catalog import (
            PostgresEphemerisSnapshotCatalog,
        )
        from leo_flow.analysis.recording import DurableFeatureSetRepository
        from leo_flow.hardware.persistence import DurableHardwareMetadataRepository
        from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
        from leo_flow.storage.recording_codec import SigMFRecordingObjectReader
    except ImportError as error:
        raise OfflineAnalysisCompositionError(
            "offline analysis PostgreSQL support requires the server dependency"
        ) from error

    dsn = job_spec.dsn

    def connect() -> psycopg.Connection[dict[str, object]]:
        connection = psycopg.connect(
            dsn,
            row_factory=dict_row,
            connect_timeout=POSTGRES_TIMEOUT_S,
            options=(
                f"-c statement_timeout={POSTGRES_TIMEOUT_S * 1000} "
                f"-c lock_timeout={POSTGRES_TIMEOUT_S * 1000}"
            ),
        )
        connection.execute("SET ROLE leo_analysis")
        return connection

    blobs = _DeferredFileSystemBlobStore(recording_spec.cas_root)
    jobs = PostgresJobLeaseRepository(connect)
    features = DurableFeatureSetRepository(blobs, PostgresFeatureSetCatalog(connect))
    components = OfflineAnalysisComponents(
        jobs=jobs,
        recordings=SigMFRecordingObjectReader(blobs),
        recording_analyzer=ExactRecordingAnalyzerRegistry(feature_spec.analyzers),
        recording_committer=AtomicPostgresRecordingAnalysisCommitter(blobs, connect),
        datasets=DurableDatasetSnapshotRepository(
            blobs, PostgresDatasetSnapshotCatalog(connect)
        ),
        features=features,
        ephemerides=_ExactArchivedEphemerisReader(
            PostgresEphemerisSnapshotCatalog(connect), blobs
        ),
        hardware=DurableHardwareMetadataRepository(
            blobs, PostgresHardwareSnapshotCatalog(connect)
        ),
        model_fitter_factory=ExactModelFitterRegistry(model_spec.fitters),
        model_committer=AtomicPostgresModelAnalysisCommitter(blobs, connect),
        preflight=lambda: _preflight_analysis(dsn, blobs),
    )
    return build_offline_analysis_service(
        config,
        components,
        lease_ttl_s=lease_ttl_s,
        diagnostics=diagnostics,
    )


def _preflight_analysis(dsn: str, blobs: _DeferredFileSystemBlobStore) -> None:
    """Prove schema receipts, role membership/privileges, and writable CAS."""

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as error:
        raise OfflineAnalysisCompositionError(
            "offline analysis PostgreSQL support requires the server dependency"
        ) from error
    with psycopg.connect(
        dsn,
        row_factory=dict_row,
        connect_timeout=POSTGRES_TIMEOUT_S,
        options=(
            f"-c statement_timeout={POSTGRES_TIMEOUT_S * 1000} "
            f"-c lock_timeout={POSTGRES_TIMEOUT_S * 1000}"
        ),
    ) as connection:
        membership = connection.execute(
            "SELECT pg_has_role(current_user, 'leo_analysis', 'MEMBER') AS member"
        ).fetchone()
        if membership is None or membership["member"] is not True:
            raise OfflineAnalysisCompositionError(
                "catalog credential is not a leo_analysis role member"
            )
        rows = connection.execute("SELECT name FROM schema_migration").fetchall()
        applied = {str(row["name"]) for row in rows}
        missing = set(EXPECTED_MIGRATIONS) - applied
        if missing:
            raise OfflineAnalysisCompositionError(
                "catalog is missing required offline-analysis migrations"
            )
        connection.execute("SET ROLE leo_analysis")
        privileges = connection.execute(
            """
            SELECT
              has_table_privilege(current_user, 'object_blob', 'SELECT,INSERT') AS objects,
              has_table_privilege(current_user, 'recording', 'SELECT') AS recordings,
              has_table_privilege(current_user, 'job', 'SELECT,INSERT,UPDATE') AS jobs,
              has_table_privilege(current_user, 'dataset_snapshot', 'SELECT') AS datasets,
              has_table_privilege(current_user, 'dataset_member', 'SELECT') AS members,
              has_table_privilege(current_user, 'feature_set', 'SELECT,INSERT') AS features,
              has_table_privilege(
                  current_user, 'recording_waterfall', 'SELECT'
              ) AS waterfalls,
              has_table_privilege(
                  current_user, 'recording_starlink_candidate', 'SELECT'
              ) AS starlink_candidates,
              has_table_privilege(
                  current_user, 'recording_starlink_detector_suite', 'SELECT'
              ) AS starlink_detector_suites,
              has_table_privilege(current_user, 'ephemeris_snapshot', 'SELECT') AS ephemerides,
              has_table_privilege(current_user, 'hardware_snapshot', 'SELECT') AS hardware,
              has_table_privilege(current_user, 'hardware_radio', 'SELECT') AS radios,
              has_table_privilege(current_user, 'hardware_receiver_chain', 'SELECT') AS chains,
              has_table_privilege(current_user, 'model_snapshot', 'SELECT,INSERT') AS models,
              NOT has_table_privilege(
                  current_user, 'feature_projection_work', 'SELECT,INSERT,UPDATE'
              ) AS projection_work_private,
              NOT has_table_privilege(
                  current_user, 'waterfall_projection_work', 'SELECT,INSERT,UPDATE'
              ) AS waterfall_projection_work_private,
              NOT has_table_privilege(
                  current_user, 'dashboard_recording_waterfall_projection',
                  'SELECT,INSERT,UPDATE'
              ) AS dashboard_waterfall_private,
              NOT has_table_privilege(
                  current_user, 'starlink_projection_work',
                  'SELECT,INSERT,UPDATE'
              ) AS starlink_projection_work_private,
              NOT has_table_privilege(
                  current_user, 'dashboard_recording_starlink_projection',
                  'SELECT,INSERT,UPDATE'
              ) AS dashboard_starlink_private,
              NOT has_table_privilege(
                  current_user, 'starlink_detector_suite_projection_work',
                  'SELECT,INSERT,UPDATE'
              ) AS starlink_suite_projection_work_private,
              NOT has_table_privilege(
                  current_user,
                  'dashboard_recording_starlink_detector_suite_projection',
                  'SELECT,INSERT,UPDATE'
              ) AS dashboard_starlink_suite_private,
              has_function_privilege(
                  current_user,
                  'publish_feature_projection_work(text,text,text,bigint,text,text,text,text,text,text,text)',
                  'EXECUTE'
              ) AS projection_publish,
              has_function_privilege(
                  current_user, 'claim_feature_projection_work(text,interval)',
                  'EXECUTE'
              ) AS projection_claim,
              has_function_privilege(
                  current_user,
                  'heartbeat_feature_projection_work(text,text,bigint,interval)',
                  'EXECUTE'
              ) AS projection_heartbeat,
              has_function_privilege(
                  current_user,
                  'complete_feature_projection_work(text,text,bigint)', 'EXECUTE'
              ) AS projection_complete,
              has_function_privilege(
                  current_user,
                  'retry_feature_projection_work(text,text,bigint,text,interval)',
                  'EXECUTE'
              ) AS projection_retry,
              has_function_privilege(
                  current_user,
                  'park_feature_projection_work(text,text,bigint,text)', 'EXECUTE'
              ) AS projection_park,
              has_function_privilege(
                  current_user,
                  'read_feature_projection_receipt(text)', 'EXECUTE'
              ) AS projection_receipt_read,
              has_function_privilege(
                  current_user,
                  'publish_recording_waterfall(text,text,text,text,text,text,text,text,text,integer,integer,text)',
                  'EXECUTE'
              ) AS waterfall_publish,
              has_function_privilege(
                  current_user,
                  'publish_waterfall_projection_work(text,text,text,text,text,text,text)',
                  'EXECUTE'
              ) AS waterfall_projection_publish,
              has_function_privilege(
                  current_user, 'claim_waterfall_projection_work(text,interval)',
                  'EXECUTE'
              ) AS waterfall_projection_claim,
              has_function_privilege(
                  current_user,
                  'heartbeat_waterfall_projection_work(text,text,bigint,interval)',
                  'EXECUTE'
              ) AS waterfall_projection_heartbeat,
              has_function_privilege(
                  current_user,
                  'complete_waterfall_projection_work(text,text,bigint)', 'EXECUTE'
              ) AS waterfall_projection_complete,
              has_function_privilege(
                  current_user,
                  'retry_waterfall_projection_work(text,text,bigint,text,interval)',
                  'EXECUTE'
              ) AS waterfall_projection_retry,
              has_function_privilege(
                  current_user,
                  'park_waterfall_projection_work(text,text,bigint,text)', 'EXECUTE'
              ) AS waterfall_projection_park,
              has_function_privilege(
                  current_user,
                  'read_waterfall_analysis_receipt(text)', 'EXECUTE'
              ) AS waterfall_receipt_read,
              has_function_privilege(
                  current_user,
                  'publish_dashboard_recording_waterfall(jsonb)', 'EXECUTE'
              ) AS dashboard_waterfall_publish,
              has_function_privilege(
                  current_user,
                  'publish_recording_starlink_candidate(text,text,text,text,text,text,text,text,integer,integer,text)',
                  'EXECUTE'
              ) AS starlink_publish,
              has_function_privilege(
                  current_user,
                  'publish_starlink_projection_work(text,text,text,bigint,text,text,text,text)',
                  'EXECUTE'
              ) AS starlink_projection_publish,
              has_function_privilege(
                  current_user, 'claim_starlink_projection_work(text,interval)',
                  'EXECUTE'
              ) AS starlink_projection_claim,
              has_function_privilege(
                  current_user,
                  'complete_starlink_projection_work(text,text,bigint)',
                  'EXECUTE'
              ) AS starlink_projection_complete,
              has_function_privilege(
                  current_user,
                  'retry_starlink_projection_work(text,text,bigint,text,interval)',
                  'EXECUTE'
              ) AS starlink_projection_retry,
              has_function_privilege(
                  current_user,
                  'park_starlink_projection_work(text,text,bigint,text)',
                  'EXECUTE'
              ) AS starlink_projection_park,
              has_function_privilege(
                  current_user, 'read_starlink_analysis_receipt(text)',
                  'EXECUTE'
              ) AS starlink_receipt_read,
              has_function_privilege(
                  current_user,
                  'publish_dashboard_recording_starlink(jsonb,text,text,bigint)',
                  'EXECUTE'
              ) AS dashboard_starlink_publish,
              has_function_privilege(
                  current_user,
                  'publish_recording_starlink_detector_suite(text,text,text,text,text,text,text,text,text,integer,integer,text)',
                  'EXECUTE'
              ) AS starlink_suite_publish,
              has_function_privilege(
                  current_user,
                  'publish_starlink_detector_suite_projection_work(text,text,text,bigint,text,text,text,text)',
                  'EXECUTE'
              ) AS starlink_suite_projection_publish,
              has_function_privilege(
                  current_user,
                  'claim_starlink_detector_suite_projection_work(text,interval)',
                  'EXECUTE'
              ) AS starlink_suite_projection_claim,
              has_function_privilege(
                  current_user,
                  'complete_starlink_detector_suite_projection_work(text,text,bigint)',
                  'EXECUTE'
              ) AS starlink_suite_projection_complete,
              has_function_privilege(
                  current_user,
                  'retry_starlink_detector_suite_projection_work(text,text,bigint,text,interval)',
                  'EXECUTE'
              ) AS starlink_suite_projection_retry,
              has_function_privilege(
                  current_user,
                  'park_starlink_detector_suite_projection_work(text,text,bigint,text)',
                  'EXECUTE'
              ) AS starlink_suite_projection_park,
              has_function_privilege(
                  current_user, 'read_starlink_detector_suite_receipt(text)',
                  'EXECUTE'
              ) AS starlink_suite_receipt_read,
              has_function_privilege(
                  current_user,
                  'publish_dashboard_recording_starlink_detector_suite(jsonb,text,text,bigint)',
                  'EXECUTE'
              ) AS dashboard_starlink_suite_publish,
              has_function_privilege(
                  current_user,
                  'claim_campaign_analysis_job(text[],text,text,interval)',
                  'EXECUTE'
              ) AS campaign_analysis_job_claim,
              has_function_privilege(
                  current_user,
                  'claim_campaign_feature_projection(text[],text,interval)',
                  'EXECUTE'
              ) AS campaign_feature_projection_claim,
              has_function_privilege(
                  current_user,
                  'claim_campaign_waterfall_projection(text[],text,interval)',
                  'EXECUTE'
              ) AS campaign_waterfall_projection_claim,
              has_function_privilege(
                  current_user,
                  'claim_campaign_starlink_suite_projection(text[],text,interval)',
                  'EXECUTE'
              ) AS campaign_starlink_suite_projection_claim,
              has_function_privilege(
                  current_user,
                  'read_campaign_analysis_lane_status(text,text[])',
                  'EXECUTE'
              ) AS campaign_analysis_lane_status_read
            """
        ).fetchone()
        if privileges is None or not all(
            value is True for value in privileges.values()
        ):
            raise OfflineAnalysisCompositionError(
                "leo_analysis database capability is incomplete"
            )
    blobs.preflight()


def build_station_plugin(
    scientific: StationScientificFactories,
    *,
    cas_root: Path,
    lease_ttl_s: float = 60.0,
) -> DeploymentPlugin:
    """Build one exact station plugin; the caller owns scientific approval.

    The returned plugin contains no capture, provider retrieval, mutable alias,
    or algorithm discovery capability.
    """

    if not cas_root.is_absolute() or lease_ttl_s <= 0:
        raise ValueError("absolute CAS root and positive lease TTL are required")
    manifest = AdapterManifest(
        {
            Process.ANALYSIS: {
                Capability.JOB_REPOSITORY: {JOB_REPOSITORY_REF: _job_spec},
                Capability.RECORDING_READER: {
                    RECORDING_READER_REF: _recording_spec(cas_root)
                },
                Capability.FEATURE_PUBLISHER: {
                    FEATURE_PUBLISHER_REF: _feature_spec(scientific.recording_analyzers)
                },
                Capability.MODEL_PUBLISHER: {
                    MODEL_PUBLISHER_REF: _model_spec(scientific.model_fitters)
                },
            }
        }
    )

    def build(
        config: ServiceConfig, adapters: AdapterSet, diagnostics: DiagnosticSink
    ) -> ServiceLoop:
        return _build_postgres_components(
            config,
            adapters,
            diagnostics,
            lease_ttl_s=lease_ttl_s,
        )

    from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider

    return DeploymentPlugin(
        manifest,
        MappingProxyType({SECRET_PROVIDER: SystemdCredentialProvider()}),
        MappingProxyType({Process.ANALYSIS: build}),
    )
