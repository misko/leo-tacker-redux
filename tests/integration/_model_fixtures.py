"""Public-contract fixtures and narrow reader fakes for integration tests."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass

from leo_flow.analysis.model import (
    ModelExecutionContext,
    ReceiverQualityAggregateConfig,
    receiver_quality_aggregate_algorithm_ref,
    receiver_quality_aggregate_config_ref,
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
    AnalysisRunId,
    DatasetSnapshotId,
    Digest,
    FeatureId,
    FeatureSetId,
    HardwareSnapshotId,
    PlanId,
    Provenance,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    StationId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.ephemeris import EphemerisSnapshotRef
from leo_flow.contracts.features import (
    FeatureObservation,
    FeatureSetBundle,
    FeatureSetRef,
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
    feature_dataset_membership_digest,
)
from leo_flow.contracts.ports import EphemerisView
from leo_flow.contracts.storage import ObjectRef

RECEIVER = ReceiverChainId("rx_vertical")


def digest(label: str) -> Digest:
    return Digest.sha256(label.encode("utf-8"))


def recording_manifest(
    index: int,
    *,
    kind: ActivityKind,
    started_utc_ns: int,
    receiver_id: ReceiverChainId = RECEIVER,
) -> RecordingManifest:
    recording_id = RecordingId(f"rec_slice_{index}")
    segment_id = SegmentId(f"seg_slice_{index}")
    request = SegmentRequest(
        segment_id=segment_id,
        center_frequency_hz=1_825_000_000.0,
        sample_rate_hz=1_000_000.0,
        bandwidth_hz=1_000_000.0,
        receiver_chain_ids=(receiver_id,),
        gain=GainSetting(GainMode.MANUAL, 40.0),
        sample_count=64,
    )
    segment = SegmentManifest(
        segment_id=segment_id,
        requested=request,
        actual_center_frequency_hz=request.center_frequency_hz,
        actual_sample_rate_hz=request.sample_rate_hz,
        actual_bandwidth_hz=request.bandwidth_hz,
        actual_gain=request.gain,
        start_utc_ns=UtcNs(started_utc_ns),
        monotonic_start_ns=index,
        sample_count=64,
        shape=(64, 1, 2),
    )
    activity = ActivityManifest(
        activity_id=ActivityId(f"act_slice_{index}"),
        kind=kind,
        started_utc_ns=UtcNs(started_utc_ns),
        finished_utc_ns=UtcNs(started_utc_ns + 1_000),
        segment_ids=(segment_id,),
    )
    return RecordingManifest(
        schema=SchemaRef(RecordingManifest.SCHEMA_ID),
        recording_id=recording_id,
        created_utc_ns=UtcNs(started_utc_ns - 1),
        capture_started_utc_ns=UtcNs(started_utc_ns),
        capture_finished_utc_ns=UtcNs(started_utc_ns + 1_000),
        station_id=StationId("station_vertical"),
        radio_id=RadioId("radio_vertical"),
        radio_serial="serial-vertical",
        receiver_chain_ids=(receiver_id,),
        clock_status="test-clock",
        hardware_metadata_snapshot_id=HardwareSnapshotId("hw_vertical"),
        activities=(activity,),
        segments=(segment,),
        plan_id=PlanId(f"plan_slice_{index}"),
        producer="integration-fixture",
    )


def feature_set(
    manifest: RecordingManifest, score: float
) -> tuple[FeatureSetRef, FeatureSetBundle]:
    suffix = str(manifest.recording_id).removeprefix("rec_slice_")
    feature_set_id = FeatureSetId(f"fset_slice_{suffix}")
    run_id = AnalysisRunId(f"arun_slice_{suffix}")
    observation = FeatureObservation(
        feature_id=FeatureId(f"feature_slice_{suffix}"),
        recording_id=manifest.recording_id,
        segment_id=manifest.segments[0].segment_id,
        method_id="sample-quality",
        method_version="0.1.0",
        window_start_sample=0,
        window_stop_sample=64,
        segment_sample_count=64,
        midpoint_utc_ns=UtcNs(int(manifest.capture_started_utc_ns) + 500),
        feature_kind="sample-quality",
        score=score,
        score_semantics="rms-magnitude-counts",
        receiver_chain_id=manifest.receiver_chain_ids[0],
        uncertainty=(("status", "descriptive-only"),),
    )
    bundle = FeatureSetBundle(
        schema=SchemaRef(FeatureSetBundle.SCHEMA_ID),
        feature_set_id=feature_set_id,
        analysis_run_id=run_id,
        recording_id=manifest.recording_id,
        input_recording_identity_digest=digest(f"recording-{manifest.recording_id}"),
        provenance=Provenance(
            producer_name="independent-recording-fixture",
            producer_version="0.1.0",
            git_commit="fixture-commit",
            environment_digest=digest("feature-environment"),
            normalized_config_digest=digest("feature-config"),
            input_digests=(digest(f"recording-{manifest.recording_id}"),),
            dependency_digests=(digest("feature-algorithm"),),
            started_utc_ns=manifest.capture_finished_utc_ns,
            completed_utc_ns=UtcNs(int(manifest.capture_finished_utc_ns) + 1),
            host_class="fixture-host",
        ),
        observations=(observation,),
        method_scores=(),
    )
    payload = canonical_json_bytes(bundle)
    bundle_ref = ObjectRef(
        digest=Digest.sha256(payload),
        byte_count=len(payload),
        media_type="application/json",
        format_id="feature-set-bundle-v0.1",
        locator=f"memory://features/{feature_set_id}",
    )
    return FeatureSetRef(feature_set_id, run_id, bundle_ref), bundle


def dataset(refs: tuple[FeatureSetRef, ...]) -> FeatureDatasetSnapshot:
    return FeatureDatasetSnapshot(
        schema=SchemaRef(FeatureDatasetSnapshot.SCHEMA_ID),
        snapshot_id=DatasetSnapshotId("dataset_vertical"),
        ordered_feature_set_refs=refs,
        selection_spec="integration:explicit-membership",
        selection_cutoff_utc_ns=UtcNs(10_000),
        membership_digest=feature_dataset_membership_digest(refs),
    )


def hardware() -> tuple[HardwareMetadataSnapshotRef, HardwareMetadataSnapshot]:
    snapshot_id = HardwareSnapshotId("hw_vertical")
    ref = HardwareMetadataSnapshotRef(snapshot_id, digest("hardware-vertical"))
    snapshot = HardwareMetadataSnapshot(
        schema=SchemaRef(HardwareMetadataSnapshot.SCHEMA_ID),
        snapshot_id=snapshot_id,
        station_id=StationId("station_vertical"),
        radio_ids=(RadioId("radio_vertical"),),
        receiver_chains=(
            ReceiverChainMetadata(
                receiver_chain_id=ReceiverChainId("rx_vertical"),
                radio_id=RadioId("radio_vertical"),
                radio_channel=0,
                lnb_id="lnb-vertical",
                polarization=None,
                cable_id=None,
                valid_from_utc_ns=UtcNs(0),
                valid_until_utc_ns=None,
            ),
        ),
    )
    return ref, snapshot


def model_request(
    snapshot: FeatureDatasetSnapshot,
    config: ReceiverQualityAggregateConfig,
    hardware_ref: HardwareMetadataSnapshotRef,
) -> ModelAnalysisRequest:
    return ModelAnalysisRequest(
        schema=SchemaRef(ModelAnalysisRequest.SCHEMA_ID),
        dataset_snapshot_ref=FeatureDatasetSnapshotRef(
            snapshot.snapshot_id, snapshot.membership_digest
        ),
        hardware_metadata_snapshot_refs=(hardware_ref,),
        ephemeris_snapshot_refs=(),
        model_config_ref=receiver_quality_aggregate_config_ref(config),
        algorithm_ref=receiver_quality_aggregate_algorithm_ref(),
    )


def execution_context() -> ModelExecutionContext:
    return ModelExecutionContext(
        producer_name="vertical-model",
        producer_version="0.1.0",
        git_commit="vertical-commit",
        environment_digest=digest("model-environment"),
        started_utc_ns=UtcNs(20_000),
        completed_utc_ns=UtcNs(20_001),
        host_class="integration-host",
    )


@dataclass
class _FeatureView:
    ref: FeatureSetRef
    value: FeatureSetBundle

    def bundle(self) -> FeatureSetBundle:
        return self.value


class _Context(AbstractContextManager[_FeatureView]):
    def __init__(self, view: _FeatureView) -> None:
        self._view = view

    def __enter__(self) -> _FeatureView:
        return self._view

    def __exit__(self, *args: object) -> None:
        return None


class FeatureReader:
    def __init__(
        self, entries: tuple[tuple[FeatureSetRef, FeatureSetBundle], ...]
    ) -> None:
        self._entries: dict[str, tuple[FeatureSetRef, FeatureSetBundle]] = {}
        self.calls: list[FeatureSetRef] = []
        for entry in entries:
            self.add(entry)

    def add(self, entry: tuple[FeatureSetRef, FeatureSetBundle]) -> None:
        ref, bundle = entry
        self._entries[str(ref.feature_set_id)] = (ref, bundle)

    def open(self, ref: FeatureSetRef) -> _Context:
        self.calls.append(ref)
        actual_ref, bundle = self._entries[str(ref.feature_set_id)]
        return _Context(_FeatureView(actual_ref, bundle))


class HardwareReader:
    def __init__(
        self,
        ref: HardwareMetadataSnapshotRef,
        snapshot: HardwareMetadataSnapshot,
    ) -> None:
        self._ref = ref
        self._snapshot = snapshot
        self.calls: list[HardwareMetadataSnapshotRef] = []

    def get(self, ref: HardwareMetadataSnapshotRef) -> HardwareMetadataSnapshot:
        self.calls.append(ref)
        if ref != self._ref:
            raise LookupError(ref)
        return self._snapshot


class NoEphemerides:
    def open(self, ref: EphemerisSnapshotRef) -> AbstractContextManager[EphemerisView]:
        raise AssertionError(f"unexpected ephemeris input: {ref}")
