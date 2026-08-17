from __future__ import annotations

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
    AnalysisRunId,
    Digest,
    FeatureId,
    FeatureSetId,
    HardwareSnapshotId,
    ModelRunId,
    ModelSnapshotId,
    PlanId,
    Provenance,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    StationId,
    UtcNs,
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.features import (
    FeatureObservation,
    FeatureSetBundle,
    FeatureSetRef,
)
from leo_flow.contracts.model import ModelSnapshotBundle, ModelSnapshotRef
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)


def digest(label: str) -> Digest:
    return Digest.sha256(label.encode())


def recording_manifest(index: int) -> RecordingManifest:
    segment_id = SegmentId(f"seg_projection_{index}")
    receiver_id = ReceiverChainId("rx_projection")
    request = SegmentRequest(
        segment_id,
        1_825_000_000.0,
        1_000_000.0,
        1_000_000.0,
        (receiver_id,),
        GainSetting(GainMode.MANUAL, 30.0),
        sample_count=64,
    )
    segment = SegmentManifest(
        segment_id,
        request,
        request.center_frequency_hz,
        request.sample_rate_hz,
        request.bandwidth_hz,
        request.gain,
        UtcNs(index * 1_000),
        index,
        64,
        (64, 1, 2),
    )
    activity = ActivityManifest(
        ActivityId(f"act_projection_{index}"),
        ActivityKind.SCAN,
        UtcNs(index * 1_000),
        UtcNs(index * 1_000 + 64_000),
        (segment_id,),
    )
    return RecordingManifest(
        SchemaRef(RecordingManifest.SCHEMA_ID),
        RecordingId(f"rec_projection_{index}"),
        UtcNs(index * 1_000 - 1),
        UtcNs(index * 1_000),
        UtcNs(index * 1_000 + 64_000),
        StationId("station_projection"),
        RadioId("radio_projection"),
        "serial-projection",
        (receiver_id,),
        "synchronized",
        HardwareSnapshotId("hw_projection"),
        (activity,),
        (segment,),
        PlanId(f"plan_projection_{index}"),
        "projection-fixture",
    )


def published_recording(manifest: RecordingManifest) -> PublishedRecordingRef:
    suffix = str(manifest.recording_id)
    data = ObjectRef(
        digest(f"{suffix}:data"),
        64,
        "application/octet-stream",
        "leo-recording-data-v1",
        f"object://{suffix}/data",
    )
    metadata = ObjectRef(
        digest(f"{suffix}:metadata"),
        128,
        "application/json",
        "leo-recording-metadata-v1",
        f"object://{suffix}/metadata",
    )
    return PublishedRecordingRef(
        RecordingObjectRef(
            manifest.recording_id, data, metadata, canonical_digest(manifest)
        )
    )


def feature_bundle_and_ref(
    recording: RecordingObjectRef,
    manifest: RecordingManifest,
    index: int,
    *,
    feature_id: FeatureId | None = None,
    score: float = 0.5,
) -> tuple[FeatureSetBundle, FeatureSetRef]:
    observation = FeatureObservation(
        feature_id or FeatureId(f"feature_projection_{index}"),
        manifest.recording_id,
        manifest.segments[0].segment_id,
        "fft-energy",
        "1.0.0",
        0,
        64,
        64,
        UtcNs(int(manifest.capture_started_utc_ns) + 50),
        "energy",
        score,
        "normalized-energy",
        receiver_chain_id=manifest.receiver_chain_ids[0],
    )
    bundle = FeatureSetBundle(
        SchemaRef(FeatureSetBundle.SCHEMA_ID),
        FeatureSetId(f"fset_projection_{index}"),
        AnalysisRunId(f"arun_projection_{index}"),
        manifest.recording_id,
        recording.identity_digest(),
        _provenance(f"feature-{index}", recording.identity_digest()),
        (observation,),
        (),
    )
    payload = canonical_json_bytes(bundle)
    object_ref = ObjectRef(
        Digest.sha256(payload),
        len(payload),
        "application/json",
        "feature-set-bundle-v0.1",
        f"object://feature/{index}",
    )
    return bundle, FeatureSetRef(
        bundle.feature_set_id, bundle.analysis_run_id, object_ref
    )


def model_bundle_and_ref(index: int) -> tuple[ModelSnapshotBundle, ModelSnapshotRef]:
    input_digest = digest(f"dataset-{index}")
    bundle = ModelSnapshotBundle(
        SchemaRef(ModelSnapshotBundle.SCHEMA_ID),
        ModelSnapshotId(f"model_projection_{index}"),
        ModelRunId(f"mrun_projection_{index}"),
        input_digest,
        (),
        (),
        _provenance(f"model-{index}", input_digest),
        (),
        ("fixture-model",),
    )
    payload = canonical_json_bytes(bundle)
    object_ref = ObjectRef(
        Digest.sha256(payload),
        len(payload),
        "application/json",
        "model-snapshot-bundle-v0.1",
        f"object://model/{index}",
    )
    return bundle, ModelSnapshotRef(
        bundle.model_snapshot_id, bundle.model_run_id, object_ref
    )


def _provenance(label: str, input_digest: Digest) -> Provenance:
    return Provenance(
        "projection-fixture",
        "1.0.0",
        "fixture-commit",
        digest(f"{label}:environment"),
        digest(f"{label}:config"),
        (input_digest,),
        (digest(f"{label}:dependency"),),
        UtcNs(1),
        UtcNs(2),
        "test-host",
    )
