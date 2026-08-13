from __future__ import annotations

import struct

from leo_flow.analysis.recording import (
    AnalysisExecutionContext,
    QualityPsdAnalyzer,
    QualityPsdConfig,
    quality_psd_algorithm_ref,
    quality_psd_config_ref,
)
from leo_flow.capture import (
    CaptureIdentity,
    FakePairedRadio,
    PlanCaptureEngine,
    Refill,
    SQLiteLocalSpool,
)
from leo_flow.contracts.capture import (
    ActivityKind,
    ActivityRequest,
    CapturePlan,
    GainMode,
    GainSetting,
    SegmentRequest,
)
from leo_flow.contracts.core import (
    ActivityId,
    Digest,
    HardwareSnapshotId,
    PlanId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    StationId,
    UtcNs,
)
from leo_flow.contracts.features import FeatureSetBundle, RecordingAnalysisRequest
from leo_flow.storage import (
    FileSystemBlobStore,
    SigMFRecordingObjectReader,
    SigMFRecordingWriter,
)
from leo_flow.storage.catalog import InMemoryRecordingCatalog, RecordingPublisherAdapter
from testkit import FakeClock


def _paired_tone(sample_count: int) -> bytes:
    words: list[int] = []
    pattern = ((1000, 0), (0, 1000), (-1000, 0), (0, -1000))
    for index in range(sample_count):
        i, q = pattern[index % len(pattern)]
        words.extend((i, q, i // 2, q // 2))
    return struct.pack(f"<{len(words)}h", *words)


def test_capture_publish_read_and_independently_analyze_one_recording(tmp_path) -> None:
    sample_count = 256
    segment = SegmentRequest(
        segment_id=SegmentId("seg_vertical"),
        center_frequency_hz=1_825_117_187.5,
        sample_rate_hz=2_500_000.0,
        bandwidth_hz=2_500_000.0,
        receiver_chain_ids=(ReceiverChainId("rx_a"), ReceiverChainId("rx_b")),
        gain=GainSetting(GainMode.MANUAL, 40.0),
        sample_count=sample_count,
    )
    plan = CapturePlan(
        schema=SchemaRef(CapturePlan.SCHEMA_ID),
        plan_id=PlanId("plan_vertical"),
        radio_id=RadioId("radio_vertical"),
        receiver_chain_ids=segment.receiver_chain_ids,
        activities=(
            ActivityRequest(ActivityId("act_vertical"), ActivityKind.DWELL, (segment,)),
        ),
    )
    clock = FakeClock()
    recording_id = RecordingId("rec_vertical")
    spool = SQLiteLocalSpool(
        tmp_path / "spool.sqlite3",
        tmp_path / "spool",
        id_factory=lambda: recording_id,
        now_ns=clock.now_utc_ns,
    )
    radio = FakePairedRadio(
        plan.radio_id,
        plan.receiver_chain_ids,
        {segment.segment_id: (Refill(_paired_tone(sample_count)),)},
        clock=clock,
    )
    engine = PlanCaptureEngine(
        CaptureIdentity(
            StationId("station_vertical"),
            "serial-vertical",
            "test-clock",
            HardwareSnapshotId("hw_vertical"),
            "vertical-test",
        ),
        clock=clock,
    )
    completed = engine.execute(plan, radio, SigMFRecordingWriter(), spool)

    blobs = FileSystemBlobStore(tmp_path / "blobs")
    catalog = InMemoryRecordingCatalog()
    published = RecordingPublisherAdapter(blobs, catalog).publish(
        completed, idempotency_key="vertical-recording"
    )
    assert catalog.get(str(recording_id)) == published

    config = QualityPsdConfig(psd_window_samples=256)
    request = RecordingAnalysisRequest(
        schema=SchemaRef(RecordingAnalysisRequest.SCHEMA_ID),
        recording_id=recording_id,
        recording_object_ref=published.recording_object,
        algorithm_ref=quality_psd_algorithm_ref(),
        config_ref=quality_psd_config_ref(config),
        dependency_refs=(),
        requested_output_schema=SchemaRef(FeatureSetBundle.SCHEMA_ID),
    )
    analyzer = QualityPsdAnalyzer(
        config,
        AnalysisExecutionContext(
            producer_name="vertical-analyzer",
            producer_version="0.1.0",
            git_commit="vertical-test",
            environment_digest=Digest.sha256(b"vertical-environment"),
            started_utc_ns=UtcNs(clock.now_utc_ns()),
            completed_utc_ns=UtcNs(clock.now_utc_ns() + 1),
            host_class="test",
        ),
    )
    with SigMFRecordingObjectReader(blobs).open(published.recording_object) as view:
        features = analyzer.analyze(view, request)

    assert features.recording_id == recording_id
    assert len(features.method_scores) == 2
    assert {score.receiver_key for score in features.method_scores} == {"rx_a", "rx_b"}
    assert {item.feature_kind for item in features.observations} == {
        "sample-quality",
        "compact-psd-peak",
    }
