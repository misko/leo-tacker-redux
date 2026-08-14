from __future__ import annotations

import io
import json
from contextlib import nullcontext
from dataclasses import dataclass

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.feature_postgres_catalog import PostgresFeatureSetCatalog
from leo_flow.adapters.model_analysis_postgres import (
    AtomicPostgresModelAnalysisCommitter,
)
from leo_flow.adapters.model_postgres_catalog import PostgresModelSnapshotCatalog
from leo_flow.adapters.recording_analysis_postgres import (
    AtomicPostgresRecordingAnalysisCommitter,
)
from leo_flow.analysis.dataset import (
    DatasetMember,
    DatasetRole,
    DatasetSnapshotBundle,
    DatasetSplit,
    DurableDatasetSnapshotRepository,
    LabelEvidence,
    LabelSource,
    TruthLabel,
    dataset_snapshot_digest,
)
from leo_flow.analysis.dataset.postgres_catalog import PostgresDatasetSnapshotCatalog
from leo_flow.analysis.model import DurableModelSnapshotRepository
from leo_flow.analysis.recording import DurableFeatureSetRepository
from leo_flow.application import DashboardProjectionStore
from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    DatasetSnapshotId,
    Digest,
    DigestAlgorithm,
    FeatureId,
    FeatureSetId,
    HardwareSnapshotId,
    JobId,
    ModelRunId,
    ModelSnapshotId,
    Provenance,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    StationId,
    UtcNs,
)
from leo_flow.contracts.features import (
    FeatureObservation,
    FeatureSetBundle,
    RecordingAnalysisRequest,
)
from leo_flow.contracts.hardware import (
    HardwareMetadataSnapshot,
    HardwareMetadataSnapshotRef,
    ReceiverChainMetadata,
)
from leo_flow.contracts.model import (
    FeatureDatasetSnapshot,
    FeatureDatasetSnapshotRef,
    ModelAnalysisRequest,
    ModelSnapshotBundle,
    ModelSnapshotRef,
    feature_dataset_membership_digest,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.dashboard import DashboardJsonApplication, JsonRequest
from leo_flow.jobs import JobType
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from leo_flow.services import (
    EphemerisLinkBackfillUnavailable,
    FencedRecordingAnalysisWorker,
    ModelAnalysisJobPreparer,
    ModelAnalysisJobProcessor,
    RecordingAnalysisJobPreparer,
    TypedAnalysisRouterCycle,
    model_analysis_payload,
    recording_analysis_payload,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog


def _connect(postgres_dsn: str):
    return lambda: psycopg.connect(postgres_dsn, row_factory=dict_row)


def _artifact(label: str) -> ArtifactRef:
    return ArtifactRef(label, Digest.sha256(label.encode()))


def _published_recording(
    postgres_dsn: str, blobs: FileSystemBlobStore
) -> RecordingObjectRef:
    data = b"deterministic vertical IQ"
    metadata = b'{"recording":"vertical"}'
    data_ref = blobs.put(
        io.BytesIO(data),
        expected_digest=Digest.sha256(data),
        expected_bytes=len(data),
        media_type="application/octet-stream",
        format_id="leo-recording-data-v1",
        idempotency_key="vertical:recording:data",
    )
    metadata_ref = blobs.put(
        io.BytesIO(metadata),
        expected_digest=Digest.sha256(metadata),
        expected_bytes=len(metadata),
        media_type="application/json",
        format_id="leo-recording-metadata-v1",
        idempotency_key="vertical:recording:metadata",
    )
    recording = RecordingObjectRef(
        RecordingId("rec_durable_vertical"),
        data_ref,
        metadata_ref,
        Digest.sha256(b"vertical-manifest"),
    )
    published = PostgresRecordingCatalog(_connect(postgres_dsn)).publish(
        recording, idempotency_key="vertical:recording"
    )
    return published.recording_object


def _recording_request(recording: RecordingObjectRef) -> RecordingAnalysisRequest:
    return RecordingAnalysisRequest(
        SchemaRef(RecordingAnalysisRequest.SCHEMA_ID),
        recording.recording_id,
        recording,
        _artifact("vertical-feature-algorithm"),
        _artifact("vertical-feature-config"),
        (),
        SchemaRef(FeatureSetBundle.SCHEMA_ID),
    )


class _RecordingReader:
    def __init__(self, expected: RecordingObjectRef) -> None:
        self.expected = expected

    def open(self, ref: RecordingObjectRef):
        assert ref == self.expected
        return nullcontext("verified-recording-view")


class _DeterministicAnalyzer:
    def analyze(self, recording, request: RecordingAnalysisRequest) -> FeatureSetBundle:
        assert recording == "verified-recording-view"
        identity = request.recording_object_ref.identity_digest()
        return FeatureSetBundle(
            SchemaRef(FeatureSetBundle.SCHEMA_ID),
            FeatureSetId("fset_durable_vertical"),
            AnalysisRunId("arun_durable_vertical"),
            request.recording_id,
            identity,
            Provenance(
                "vertical-analyzer",
                "1.0",
                "vertical-commit",
                Digest.sha256(b"feature-environment"),
                request.config_ref.digest,
                (identity,),
                (request.algorithm_ref.digest,),
                UtcNs(100),
                UtcNs(101),
                "integration-host",
            ),
            (
                FeatureObservation(
                    FeatureId("feature_durable_vertical"),
                    request.recording_id,
                    SegmentId("seg_durable_vertical"),
                    "sample-quality",
                    "0.1.0",
                    0,
                    64,
                    64,
                    UtcNs(1_000),
                    "sample-quality",
                    12.5,
                    "rms-magnitude-counts",
                    receiver_chain_id=ReceiverChainId("rx_durable_vertical"),
                ),
            ),
            (),
        )


def _dataset(feature_ref) -> DatasetSnapshotBundle:
    feature_dataset = FeatureDatasetSnapshot(
        SchemaRef(FeatureDatasetSnapshot.SCHEMA_ID),
        DatasetSnapshotId("dataset_durable_vertical"),
        (feature_ref,),
        "vertical:exact-member",
        UtcNs(2_000),
        feature_dataset_membership_digest((feature_ref,)),
    )
    truth = TruthLabel(
        None,
        LabelSource.UNLABELED,
        (
            LabelEvidence(
                LabelSource.UNLABELED,
                Digest.sha256(b"vertical-unlabeled-evidence"),
                "vertical-labeler",
                1_500,
                (),
            ),
        ),
    )
    members = (
        DatasetMember(
            feature_ref,
            "vertical-pass",
            DatasetSplit.TRAIN,
            DatasetRole.CONTEXT_ONLY,
            truth,
        ),
    )
    warnings = ("unlabeled-context-only",)
    return DatasetSnapshotBundle(
        SchemaRef(DatasetSnapshotBundle.SCHEMA_ID),
        feature_dataset,
        "sample-quality",
        members,
        dataset_snapshot_digest(
            feature_dataset, "sample-quality", members, False, warnings
        ),
        False,
        warnings,
    )


@dataclass(frozen=True)
class _HardwareReader:
    ref: HardwareMetadataSnapshotRef

    def get(self, ref: HardwareMetadataSnapshotRef) -> HardwareMetadataSnapshot:
        assert ref == self.ref
        # The deterministic fitter below does not inspect the body.
        return HardwareMetadataSnapshot(
            SchemaRef(HardwareMetadataSnapshot.SCHEMA_ID),
            ref.snapshot_id,
            StationId("station_durable_vertical"),
            (RadioId("radio_durable_vertical"),),
            (
                ReceiverChainMetadata(
                    ReceiverChainId("rx_durable_vertical"),
                    RadioId("radio_durable_vertical"),
                    0,
                    "lnb-durable-vertical",
                    None,
                    None,
                    UtcNs(0),
                    None,
                ),
            ),
        )


class _NoEphemerides:
    def open(self, ref):
        raise AssertionError(f"unexpected ephemeris read: {ref}")


class _DeterministicFitter:
    def __init__(self, dataset: FeatureDatasetSnapshot) -> None:
        self.dataset = dataset

    def fit(self, request, features, ephemerides, hardware) -> ModelSnapshotBundle:
        del ephemerides
        feature_ref = self.dataset.ordered_feature_set_refs[0]
        with features.open(feature_ref) as view:
            assert view.ref == feature_ref
            feature_bundle = view.bundle()
        hardware_ref = request.hardware_metadata_snapshot_refs[0]
        assert hardware.get(hardware_ref).snapshot_id == hardware_ref.snapshot_id
        return ModelSnapshotBundle(
            SchemaRef(ModelSnapshotBundle.SCHEMA_ID),
            ModelSnapshotId("model_durable_vertical"),
            ModelRunId("mrun_durable_vertical"),
            self.dataset.membership_digest,
            (hardware_ref.digest,),
            (),
            Provenance(
                "vertical-fitter",
                "1.0",
                "vertical-commit",
                Digest.sha256(b"model-environment"),
                request.model_config_ref.digest,
                (self.dataset.membership_digest, feature_ref.bundle_ref.digest),
                (request.algorithm_ref.digest, hardware_ref.digest),
                UtcNs(200),
                UtcNs(201),
                "integration-host",
            ),
            (),
            (f"feature-score:{feature_bundle.observations[0].score}",),
        )


class _UnusedExecutor:
    def execute(self, lease):
        raise AssertionError(f"unexpected routed job: {lease.job_type}")


@pytest.mark.integration
def test_durable_recording_feature_dataset_model_dashboard_vertical(
    postgres_dsn: str, tmp_path
) -> None:
    blobs = FileSystemBlobStore(tmp_path / "cas")
    connect = _connect(postgres_dsn)
    jobs = PostgresJobLeaseRepository(connect)
    recording = _published_recording(postgres_dsn, blobs)
    request = _recording_request(recording)
    jobs.enqueue(
        JobId("job_durable_recording"),
        JobType.RECORDING_ANALYSIS,
        recording_analysis_payload(request),
    )
    feature_repository = DurableFeatureSetRepository(
        blobs, PostgresFeatureSetCatalog(connect)
    )
    recording_executor = FencedRecordingAnalysisWorker(
        jobs,
        RecordingAnalysisJobPreparer(
            _RecordingReader(recording), _DeterministicAnalyzer()
        ),
        AtomicPostgresRecordingAnalysisCommitter(blobs, connect),
        worker_id="vertical-recording",
        lease_ttl_s=30,
    )
    unused = _UnusedExecutor()
    backfill = EphemerisLinkBackfillUnavailable(jobs)
    router = TypedAnalysisRouterCycle(
        jobs,
        recording_analysis=recording_executor,
        model_analysis=unused,
        ephemeris_retrieval=unused,
        ephemeris_link_backfill=backfill,
        worker_id="vertical-router",
        lease_ttl_s=30,
    )
    assert router.process_one_job()

    with psycopg.connect(postgres_dsn) as connection:
        feature_row = connection.execute(
            "SELECT f.feature_set_id, f.analysis_run_id, b.digest_value, "
            "b.byte_count, b.media_type, b.format_id, b.locator "
            "FROM feature_set f JOIN object_blob b ON "
            "(b.digest_algorithm, b.digest_value) = "
            "(f.bundle_digest_algorithm, f.bundle_digest_value)"
        ).fetchone()
    assert feature_row[:2] == ("fset_durable_vertical", "arun_durable_vertical")
    from leo_flow.contracts.features import FeatureSetRef

    feature_ref = FeatureSetRef(
        FeatureSetId(feature_row[0]),
        AnalysisRunId(feature_row[1]),
        ObjectRef(
            Digest(DigestAlgorithm.SHA256, feature_row[2]),
            feature_row[3],
            feature_row[4],
            feature_row[5],
            feature_row[6],
        ),
    )
    with feature_repository.open(feature_ref) as view:
        assert view.bundle().feature_set_id == feature_ref.feature_set_id

    dataset = _dataset(feature_ref)
    dataset_repository = DurableDatasetSnapshotRepository(
        blobs, PostgresDatasetSnapshotCatalog(connect)
    )
    assert (
        dataset_repository.publish(dataset, idempotency_key="vertical:dataset")
        == dataset.ref
    )
    assert dataset_repository.get(dataset.ref) == dataset
    restarted_dataset_repository = DurableDatasetSnapshotRepository(
        blobs, PostgresDatasetSnapshotCatalog(connect)
    )
    assert (
        restarted_dataset_repository.publish(
            dataset, idempotency_key="vertical:dataset"
        )
        == dataset.ref
    )

    hardware_ref = HardwareMetadataSnapshotRef(
        HardwareSnapshotId("hw_durable_vertical"),
        Digest.sha256(b"vertical-hardware"),
    )
    model_request = ModelAnalysisRequest(
        SchemaRef(ModelAnalysisRequest.SCHEMA_ID),
        FeatureDatasetSnapshotRef(
            dataset.feature_dataset.snapshot_id,
            dataset.feature_dataset.membership_digest,
        ),
        (hardware_ref,),
        (),
        _artifact("vertical-model-config"),
        _artifact("vertical-model-algorithm"),
    )
    jobs.enqueue(
        JobId("job_durable_model"),
        JobType.MODEL_ANALYSIS,
        model_analysis_payload(model_request, dataset.ref),
    )
    model_executor = ModelAnalysisJobProcessor(
        ModelAnalysisJobPreparer(
            dataset_repository,
            feature_repository,
            _NoEphemerides(),
            _HardwareReader(hardware_ref),
            _DeterministicFitter,
        ),
        AtomicPostgresModelAnalysisCommitter(blobs, connect),
    )
    restarted_router = TypedAnalysisRouterCycle(
        jobs,
        recording_analysis=recording_executor,
        model_analysis=model_executor,
        ephemeris_retrieval=unused,
        ephemeris_link_backfill=backfill,
        worker_id="vertical-router-restart",
        lease_ttl_s=30,
    )
    assert restarted_router.process_one_job()
    assert not restarted_router.process_one_job()

    with psycopg.connect(postgres_dsn) as connection:
        model_row = connection.execute(
            "SELECT m.model_snapshot_id, m.model_run_id, b.digest_value, "
            "b.byte_count, b.media_type, b.format_id, b.locator "
            "FROM model_snapshot m JOIN object_blob b ON "
            "(b.digest_algorithm, b.digest_value) = "
            "(m.bundle_digest_algorithm, m.bundle_digest_value)"
        ).fetchone()
        counts = connection.execute(
            "SELECT (SELECT count(*) FROM recording), "
            "(SELECT count(*) FROM feature_set), "
            "(SELECT count(*) FROM dataset_snapshot), "
            "(SELECT count(*) FROM model_snapshot), "
            "(SELECT count(*) FROM job WHERE state = 'succeeded')"
        ).fetchone()
    assert model_row[:2] == ("model_durable_vertical", "mrun_durable_vertical")
    assert counts == (1, 1, 1, 1, 2)

    model_ref = ModelSnapshotRef(
        ModelSnapshotId(model_row[0]),
        ModelRunId(model_row[1]),
        ObjectRef(
            Digest(DigestAlgorithm.SHA256, model_row[2]),
            model_row[3],
            model_row[4],
            model_row[5],
            model_row[6],
        ),
    )
    model_repository = DurableModelSnapshotRepository(
        blobs, PostgresModelSnapshotCatalog(connect)
    )
    with model_repository.open(model_ref) as view:
        model_bundle = view.bundle()
    projections = DashboardProjectionStore()
    projections.project_model(model_bundle, model_ref)
    response = DashboardJsonApplication(projections.repository()).handle(
        JsonRequest("GET", "/api/models/model_durable_vertical", {})
    )
    assert response.status == 200
    assert json.loads(response.body)["model_snapshot_id"] == "model_durable_vertical"
