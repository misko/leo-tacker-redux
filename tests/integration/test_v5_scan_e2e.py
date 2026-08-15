from __future__ import annotations

import struct

from leo_flow.analysis.recording import (
    AnalysisExecutionContext,
    DurableFeatureSetRepository,
    QualityPsdAnalyzer,
    QualityPsdConfig,
    quality_psd_algorithm_ref,
    quality_psd_config_ref,
)
from leo_flow.analysis.recording.persistence import (
    CatalogedFeatureSet,
    FeatureSetCatalogProjection,
)
from leo_flow.capture import (
    CaptureIdentity,
    FakeV5PairedRadio,
    PlanCaptureEngine,
    PublicationReconciler,
    SQLiteLocalSpool,
    V5Refill,
)
from leo_flow.capture.scan_plan import (
    StarlinkEdgeScanSpec,
    build_starlink_edge_scan_plan,
)
from leo_flow.contracts.capture import GainMode, GainSetting
from leo_flow.contracts.continuity import (
    CaptureProvenance,
    ContinuityPolicy,
    ContinuityStatus,
    RefillMetadata,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    HardwareSnapshotId,
    PlanId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    StationId,
    UtcNs,
)
from leo_flow.contracts.features import FeatureSetBundle, FeatureSetRef
from leo_flow.contracts.storage import PublishedRecordingRef, RecordingObjectRef
from leo_flow.deployments.v5_scan_e2e import _frame_accounting, _object_integrity
from leo_flow.jobs import InMemoryJobLeaseRepository, JobState
from leo_flow.services.recording_analysis import (
    FencedRecordingAnalysisWorker,
    PreparedRecordingAnalysis,
    RecordingAnalysisJobPreparer,
)
from leo_flow.services.recording_submission import (
    RecordingAnalysisSubmission,
    RecordingAnalysisSubmissionService,
)
from leo_flow.storage import (
    FileSystemBlobStore,
    RootedSigMFRecordingStore,
    SigMFRecordingObjectReader,
    SigMFRecordingWriter,
)
from leo_flow.storage.catalog import InMemoryRecordingCatalog, RecordingPublisherAdapter
from testkit import FakeClock

RADIO = RadioId("radio_v5_scan_e2e")
RECEIVERS = (ReceiverChainId("rx_v5_1"), ReceiverChainId("rx_v5_2"))
RECORDING = RecordingId("rec_v5_scan_e2e")


def _plan():
    return build_starlink_edge_scan_plan(
        StarlinkEdgeScanSpec(
            plan_id=PlanId("plan_v5_scan_e2e"),
            radio_id=RADIO,
            receiver_chain_ids=RECEIVERS,
            gain=GainSetting(GainMode.AGC),
            sample_rate_hz=2_500_000.0,
            bandwidth_hz=2_000_000.0,
            sample_count=256,
            edge_order="U",
            edge_order_draw_u32=1,
            arm_name="e2e-256-sample",
        )
    )


def _tone(sample_count: int, phase: int) -> bytes:
    pattern = ((1000, 0), (0, 1000), (-1000, 0), (0, -1000))
    words: list[int] = []
    for index in range(sample_count):
        i, q = pattern[(index + phase) % len(pattern)]
        words.extend((i, q, i // 2, q // 2))
    return struct.pack(f"<{len(words)}h", *words)


def _metadata(segment_index: int, sample_count: int) -> RefillMetadata:
    start = 1_700_000_000_000_000_000 + segment_index * 1_000_000
    return RefillMetadata(
        refill_index=0,
        segment_sample_offset=0,
        sample_count=sample_count,
        stream_id=100 + segment_index,
        buffer_sequence=200 + segment_index,
        first_sample_sequence=segment_index * sample_count,
        monotonic_start_ns=10_000 + segment_index * 1_000,
        monotonic_end_ns=10_500 + segment_index * 1_000,
        utc_start_ns=start,
        utc_end_ns=start + 500,
        time_uncertainty_ns=50,
        gain_db_start=(40.0, 41.0),
        gain_db_end=(40.0, 41.0),
        rssi_db_start=(-50.0, -51.0),
        rssi_db_end=(-50.0, -51.0),
    )


def _capture(tmp_path):
    plan = _plan()
    scripts = {
        segment.segment_id: (V5Refill(_tone(256, index), _metadata(index, 256)),)
        for index, segment in enumerate(plan.activities[0].segments)
    }
    clock = FakeClock()
    radio = FakeV5PairedRadio(
        RADIO,
        RECEIVERS,
        scripts,
        CaptureProvenance("v5", "commit", "0.25", "v3", "metadata=1"),
        continuity_policy=ContinuityPolicy.REQUIRE_CONTIGUOUS,
        clock=clock,
    )
    spool = SQLiteLocalSpool(
        tmp_path / "capture.sqlite3",
        tmp_path / "recordings",
        id_factory=lambda: RECORDING,
        now_ns=clock.now_utc_ns,
    )
    completed = PlanCaptureEngine(
        CaptureIdentity(
            StationId("station_e2e"),
            "serial-e2e",
            "test-clock",
            HardwareSnapshotId("hw_e2e"),
            "v5-scan-e2e",
        ),
        clock=clock,
    ).execute(plan, radio, SigMFRecordingWriter(), spool)
    return plan, spool, completed


class _FeatureCatalog:
    def __init__(self) -> None:
        self.entry: CatalogedFeatureSet | None = None

    def publish(
        self,
        projection: FeatureSetCatalogProjection,
        bundle_ref,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> FeatureSetRef:
        del recording_ref, idempotency_key
        entry = CatalogedFeatureSet(projection, bundle_ref)
        if self.entry is not None and self.entry != entry:
            raise RuntimeError("feature publication conflict")
        self.entry = entry
        return entry.ref

    def get(self, ref: FeatureSetRef) -> CatalogedFeatureSet | None:
        return self.entry if self.entry is not None and self.entry.ref == ref else None


class _FeatureCommitter:
    def __init__(self, jobs, repository) -> None:
        self.jobs = jobs
        self.repository = repository
        self.ref: FeatureSetRef | None = None

    def commit(self, lease, prepared: PreparedRecordingAnalysis) -> ArtifactRef:
        self.ref = self.repository.publish(
            prepared.request,
            prepared.bundle,
            idempotency_key=f"recording-analysis:{lease.job_id}",
        )
        result = ArtifactRef(
            str(self.ref.feature_set_id),
            self.ref.bundle_ref.digest,
            prepared.bundle.schema,
        )
        self.jobs.complete(
            lease.job_id, lease.lease_token, lease.lease_generation, result
        )
        return result


def test_scan_capture_publish_submit_analyze_and_restart_without_recapture(
    tmp_path,
) -> None:
    plan, spool, completed = _capture(tmp_path)
    assert [item.segment_id for item in completed.manifest.segments] == [
        item.segment_id for item in plan.activities[0].segments
    ]
    assert all(item.sample_count == 256 for item in completed.manifest.segments)

    local = RootedSigMFRecordingStore(tmp_path / "recordings")
    blobs = FileSystemBlobStore(tmp_path / "cas")
    recording_catalog = InMemoryRecordingCatalog()
    publication = PublicationReconciler(
        spool,
        RecordingPublisherAdapter(local, blobs, recording_catalog),
        local,
    ).reconcile()
    assert (publication.published, publication.cleaned, publication.deferred) == (
        1,
        1,
        0,
    )
    published = recording_catalog.get(str(RECORDING))
    assert isinstance(published, PublishedRecordingRef)

    restarted = SQLiteLocalSpool(tmp_path / "capture.sqlite3", tmp_path / "recordings")
    assert restarted.has_durable_recording(plan.plan_id)
    assert restarted.pending_publication() == ()

    config = QualityPsdConfig(psd_window_samples=64, psd_stride_samples=128)
    jobs = InMemoryJobLeaseRepository(now_utc_ns=lambda: 100)
    submitted = RecordingAnalysisSubmissionService(jobs).submit(
        RecordingAnalysisSubmission(
            published,
            quality_psd_algorithm_ref(),
            quality_psd_config_ref(config),
            (),
            SchemaRef(FeatureSetBundle.SCHEMA_ID),
        )
    )
    feature_catalog = _FeatureCatalog()
    features = DurableFeatureSetRepository(blobs, feature_catalog)
    committer = _FeatureCommitter(jobs, features)
    worker = FencedRecordingAnalysisWorker(
        jobs,
        RecordingAnalysisJobPreparer(
            SigMFRecordingObjectReader(blobs),
            QualityPsdAnalyzer(
                config,
                AnalysisExecutionContext(
                    "e2e-analyzer",
                    "0.1.0",
                    "e2e",
                    Digest.sha256(b"e2e-environment"),
                    UtcNs(100),
                    UtcNs(101),
                    "test-host",
                ),
                read_chunk_samples=128,
            ),
        ),
        committer,
        worker_id="e2e-worker",
        lease_ttl_s=30,
    )
    assert worker.process_one_job()
    assert jobs.snapshot(submitted.job_id).state is JobState.SUCCEEDED
    assert committer.ref is not None
    with features.open(committer.ref) as view:
        bundle = view.bundle()
    assert bundle.recording_id == RECORDING
    # Two whole-span quality scores and four aligned PSD windows per segment.
    assert len(bundle.method_scores) == 48
    assert {score.segment_id for score in bundle.method_scores} == {
        segment.segment_id for segment in plan.activities[0].segments
    }
    with SigMFRecordingObjectReader(blobs).open(
        published.recording_object
    ) as recording_view:
        for segment in completed.manifest.segments:
            continuity = recording_view.continuity(segment.segment_id)
            assert continuity is not None
            assert continuity.status is ContinuityStatus.VERIFIED
            accounting = _frame_accounting(segment, continuity)
            assert accounting["refill_count"] == 1
            assert accounting["stored_sample_count"] == 256
            assert accounting["gap_count"] == 0
            assert accounting["missing_buffer_count"] == 0
            assert accounting["missing_sample_count"] == 0
            assert accounting["flags"] == []

    integrity = _object_integrity(
        blobs, published.recording_object, completed.manifest.segments
    )
    assert integrity["data"] == {
        "digest": str(published.recording_object.data_object.digest),
        "byte_count": 8 * 256 * 2 * 2 * 2,
        "verified": True,
    }
    assert integrity["metadata"] == {
        "digest": str(published.recording_object.metadata_object.digest),
        "byte_count": published.recording_object.metadata_object.byte_count,
        "verified": True,
    }
    assert integrity["expected_data_byte_count"] == 8 * 256 * 2 * 2 * 2


def test_publication_outage_leaves_completed_scan_for_restart_retry(tmp_path) -> None:
    plan, spool, _ = _capture(tmp_path)

    class Offline:
        def publish(self, recording, *, idempotency_key):
            del recording, idempotency_key
            raise OSError("analysis host unavailable")

    local = RootedSigMFRecordingStore(tmp_path / "recordings")
    first = PublicationReconciler(spool, Offline(), local).reconcile()
    assert first.deferred == 1
    restarted = SQLiteLocalSpool(tmp_path / "capture.sqlite3", tmp_path / "recordings")
    assert restarted.has_durable_recording(plan.plan_id)

    catalog = InMemoryRecordingCatalog()
    second = PublicationReconciler(
        restarted,
        RecordingPublisherAdapter(
            local, FileSystemBlobStore(tmp_path / "cas"), catalog
        ),
        local,
    ).reconcile()
    assert (second.published, second.cleaned, second.deferred) == (1, 1, 0)
    assert catalog.get(str(RECORDING)) is not None
