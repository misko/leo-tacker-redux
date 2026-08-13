"""Independent recording-analysis fakes; no production recording writer is used."""

from __future__ import annotations

from dataclasses import dataclass

from leo_flow.analysis.recording import (
    AnalysisExecutionContext,
    QualityPsdConfig,
    quality_psd_algorithm_ref,
    quality_psd_config_ref,
)
from leo_flow.contracts.capture import (
    ActivityKind,
    ActivityManifest,
    GainMode,
    GainSetting,
    RecordingManifest,
    SegmentManifest,
    SegmentRequest,
)
from leo_flow.contracts.core import (
    ActivityId,
    ArtifactRef,
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
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef

RX_IDS = (ReceiverChainId("rx_0"), ReceiverChainId("rx_1"))
DEFAULT_RECORDING_ID = RecordingId("rec_synthetic")


@dataclass(frozen=True)
class SegmentFixture:
    data: bytes
    sample_rate_hz: int
    center_frequency_hz: int = 1_000_000_000


class FakeRecordingView:
    """Only the two RecordingView capabilities, plus read-call observation."""

    def __init__(
        self,
        manifest: RecordingManifest,
        data: dict[SegmentId, bytes],
        *,
        truncate_reads: bool = False,
        mutable_result: bool = False,
    ) -> None:
        self._manifest = manifest
        self._data = data
        self.truncate_reads = truncate_reads
        self.mutable_result = mutable_result
        self.calls: list[tuple[SegmentId, int, int]] = []

    @property
    def manifest(self) -> RecordingManifest:
        return self._manifest

    def read_iq_bytes(
        self, segment_id: SegmentId, start_sample: int, stop_sample: int
    ) -> bytes:
        self.calls.append((segment_id, start_sample, stop_sample))
        raw = self._data[segment_id]
        if not 0 <= start_sample < stop_sample <= len(raw) // 8:
            raise ValueError("fake received an out-of-bounds request")
        result = raw[start_sample * 8 : stop_sample * 8]
        if self.truncate_reads:
            result = result[:-2]
        if self.mutable_result:
            return bytearray(result)  # type: ignore[return-value]
        return result


def make_view(
    *segments: SegmentFixture,
    recording_id: RecordingId = DEFAULT_RECORDING_ID,
) -> tuple[FakeRecordingView, RecordingObjectRef]:
    segment_manifests: list[SegmentManifest] = []
    activity_manifests: list[ActivityManifest] = []
    data: dict[SegmentId, bytes] = {}
    recording_start = 1_800_000_000_000_000_000
    for index, fixture in enumerate(segments):
        segment_id = SegmentId(f"seg_{index:02d}")
        sample_count, remainder = divmod(len(fixture.data), 8)
        if remainder:
            raise ValueError("fixture is not paired CI16")
        request = SegmentRequest(
            segment_id=segment_id,
            center_frequency_hz=float(fixture.center_frequency_hz),
            sample_rate_hz=float(fixture.sample_rate_hz),
            bandwidth_hz=float(fixture.sample_rate_hz),
            receiver_chain_ids=RX_IDS,
            gain=GainSetting(GainMode.MANUAL, 20.0),
            sample_count=sample_count,
        )
        start = recording_start + index * 10_000_000_000
        segment_manifests.append(
            SegmentManifest(
                segment_id=segment_id,
                requested=request,
                actual_center_frequency_hz=float(fixture.center_frequency_hz),
                actual_sample_rate_hz=float(fixture.sample_rate_hz),
                actual_bandwidth_hz=float(fixture.sample_rate_hz),
                actual_gain=request.gain,
                start_utc_ns=UtcNs(start),
                monotonic_start_ns=index * 10_000_000_000,
                sample_count=sample_count,
                shape=(sample_count, 2, 2),
            )
        )
        activity_manifests.append(
            ActivityManifest(
                activity_id=ActivityId(f"act_{index:02d}"),
                kind=ActivityKind.DWELL,
                started_utc_ns=UtcNs(start),
                finished_utc_ns=UtcNs(
                    start + max(1, round(sample_count * 1e9 / fixture.sample_rate_hz))
                ),
                segment_ids=(segment_id,),
            )
        )
        data[segment_id] = fixture.data
    if not segments:
        raise ValueError("at least one segment fixture is required")
    finish = max(activity.finished_utc_ns for activity in activity_manifests)
    manifest = RecordingManifest(
        schema=SchemaRef(RecordingManifest.SCHEMA_ID),
        recording_id=recording_id,
        created_utc_ns=UtcNs(recording_start - 1_000_000),
        capture_started_utc_ns=UtcNs(recording_start),
        capture_finished_utc_ns=UtcNs(finish),
        station_id=StationId("station_synthetic"),
        radio_id=RadioId("radio_synthetic"),
        radio_serial="independent-fixture",
        receiver_chain_ids=RX_IDS,
        clock_status="synthetic-exact",
        hardware_metadata_snapshot_id=HardwareSnapshotId("hw_synthetic"),
        activities=tuple(activity_manifests),
        segments=tuple(segment_manifests),
        plan_id=PlanId("plan_synthetic"),
        producer="independent-test-fixture",
    )
    all_data = b"".join(fixture.data for fixture in segments)
    data_ref = ObjectRef(
        digest=Digest.sha256(all_data),
        byte_count=len(all_data),
        media_type="application/vnd.sigmf.data",
        format_id="sigmf-ci16-le-v1",
        locator="fake:data",
    )
    metadata_bytes = b"independently-constructed-metadata"
    metadata_ref = ObjectRef(
        digest=Digest.sha256(metadata_bytes),
        byte_count=len(metadata_bytes),
        media_type="application/vnd.sigmf.meta+json",
        format_id="sigmf-meta-v1",
        locator="fake:metadata",
    )
    recording_ref = RecordingObjectRef(
        recording_id=recording_id,
        data_object=data_ref,
        metadata_object=metadata_ref,
        manifest_digest=Digest.sha256(b"independent-manifest"),
    )
    return FakeRecordingView(manifest, data), recording_ref


def execution_context() -> AnalysisExecutionContext:
    return AnalysisExecutionContext(
        producer_name="leo-flow-quality-psd",
        producer_version="0.1.0",
        git_commit="test-commit",
        environment_digest=Digest.sha256(b"test-environment"),
        started_utc_ns=UtcNs(1_800_000_100_000_000_000),
        completed_utc_ns=UtcNs(1_800_000_101_000_000_000),
        host_class="test-host",
    )


def make_request(
    recording_ref: RecordingObjectRef,
    config: QualityPsdConfig,
    *,
    dependencies: tuple[ArtifactRef, ...] = (),
) -> RecordingAnalysisRequest:
    return RecordingAnalysisRequest(
        schema=SchemaRef(RecordingAnalysisRequest.SCHEMA_ID),
        recording_id=recording_ref.recording_id,
        recording_object_ref=recording_ref,
        algorithm_ref=quality_psd_algorithm_ref(),
        config_ref=quality_psd_config_ref(config),
        dependency_refs=dependencies,
        requested_output_schema=SchemaRef(FeatureSetBundle.SCHEMA_ID),
    )
