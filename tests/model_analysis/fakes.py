"""Independent immutable-reader fakes for cross-recording model tests."""

from __future__ import annotations

from dataclasses import dataclass, replace

from leo_flow.analysis.model import (
    ModelExecutionContext,
    ReceiverQualityAggregateConfig,
    receiver_quality_aggregate_algorithm_ref,
    receiver_quality_aggregate_config_ref,
)
from leo_flow.contracts.core import (
    AnalysisRunId,
    DatasetSnapshotId,
    Digest,
    EphemerisSnapshotId,
    FeatureId,
    FeatureSetId,
    HardwareSnapshotId,
    Provenance,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    StationId,
    UtcNs,
)
from leo_flow.contracts.ephemeris import EphemerisSnapshotRef, EphemerisSource
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
from leo_flow.contracts.storage import ObjectRef


def digest(label: str) -> Digest:
    return Digest.sha256(label.encode("utf-8"))


def execution_context() -> ModelExecutionContext:
    return ModelExecutionContext(
        producer_name="model-test",
        producer_version="0.1.0",
        git_commit="0123456789abcdef",
        environment_digest=digest("environment"),
        started_utc_ns=UtcNs(10_000),
        completed_utc_ns=UtcNs(20_000),
        host_class="test-host",
    )


def feature_set(
    index: int,
    scores: tuple[tuple[str, float, float | None], ...],
    *,
    midpoint_utc_ns: int = 1_000,
    duplicate_first: bool = False,
) -> tuple[FeatureSetRef, FeatureSetBundle]:
    feature_set_id = FeatureSetId(f"fset_{index}")
    analysis_run_id = AnalysisRunId(f"arun_{index}")
    recording_id = RecordingId(f"rec_{index}")
    observations: list[FeatureObservation] = []
    for observation_index, (receiver, score, variance) in enumerate(scores):
        uncertainty = () if variance is None else (("score_variance", variance),)
        observation = FeatureObservation(
            feature_id=FeatureId(f"feature_{index}_{observation_index}"),
            recording_id=recording_id,
            segment_id=SegmentId(f"seg_{index}_{observation_index}"),
            method_id="sample-quality",
            method_version="0.1.0",
            window_start_sample=0,
            window_stop_sample=64,
            segment_sample_count=64,
            midpoint_utc_ns=UtcNs(midpoint_utc_ns),
            feature_kind="sample-quality",
            score=score,
            score_semantics="rms-magnitude-counts",
            receiver_chain_id=ReceiverChainId(receiver),
            uncertainty=uncertainty,
        )
        observations.append(observation)
    if duplicate_first:
        original = observations[0]
        observations.append(
            replace(
                original,
                feature_id=FeatureId(f"feature_{index}_duplicate"),
                segment_id=SegmentId(f"seg_{index}_duplicate"),
            )
        )
    bundle_digest = digest(f"feature-bundle-{index}")
    bundle_ref = ObjectRef(
        digest=bundle_digest,
        byte_count=100 + index,
        media_type="application/json",
        format_id="feature-bundle-v0.1",
        locator=f"memory://feature/{index}",
    )
    ref = FeatureSetRef(feature_set_id, analysis_run_id, bundle_ref)
    provenance = Provenance(
        producer_name="fixture",
        producer_version="0.1.0",
        git_commit="fixture-commit",
        environment_digest=digest("feature-environment"),
        normalized_config_digest=digest("feature-config"),
        input_digests=(digest(f"recording-{index}"),),
        dependency_digests=(digest("feature-algorithm"),),
        started_utc_ns=UtcNs(1),
        completed_utc_ns=UtcNs(2),
        host_class="fixture-host",
    )
    bundle = FeatureSetBundle(
        schema=SchemaRef(FeatureSetBundle.SCHEMA_ID),
        feature_set_id=feature_set_id,
        analysis_run_id=analysis_run_id,
        recording_id=recording_id,
        input_recording_identity_digest=digest(f"recording-{index}"),
        provenance=provenance,
        observations=tuple(observations),
        method_scores=(),
    )
    return ref, bundle


def dataset(refs: tuple[FeatureSetRef, ...]) -> FeatureDatasetSnapshot:
    return FeatureDatasetSnapshot(
        schema=SchemaRef(FeatureDatasetSnapshot.SCHEMA_ID),
        snapshot_id=DatasetSnapshotId("dataset_test"),
        ordered_feature_set_refs=refs,
        selection_spec="fixture:explicit-membership",
        selection_cutoff_utc_ns=UtcNs(5_000),
        membership_digest=feature_dataset_membership_digest(refs),
    )


def hardware_snapshot(
    index: int = 0,
    receivers: tuple[str, ...] = ("rx_0",),
    *,
    valid_from_utc_ns: int = 0,
    valid_until_utc_ns: int | None = None,
) -> tuple[HardwareMetadataSnapshotRef, HardwareMetadataSnapshot]:
    snapshot_id = HardwareSnapshotId(f"hw_{index}")
    radio_id = RadioId(f"radio_{index}")
    ref = HardwareMetadataSnapshotRef(snapshot_id, digest(f"hardware-{index}"))
    snapshot = HardwareMetadataSnapshot(
        schema=SchemaRef(HardwareMetadataSnapshot.SCHEMA_ID),
        snapshot_id=snapshot_id,
        station_id=StationId("station_test"),
        radio_ids=(radio_id,),
        receiver_chains=tuple(
            ReceiverChainMetadata(
                receiver_chain_id=ReceiverChainId(receiver),
                radio_id=radio_id,
                radio_channel=receiver_index,
                lnb_id=f"lnb-{index}-{receiver_index}",
                polarization=None,
                cable_id=None,
                valid_from_utc_ns=UtcNs(valid_from_utc_ns),
                valid_until_utc_ns=(
                    UtcNs(valid_until_utc_ns)
                    if valid_until_utc_ns is not None
                    else None
                ),
            )
            for receiver_index, receiver in enumerate(receivers)
        ),
    )
    return ref, snapshot


def ephemeris_ref(index: int = 0) -> EphemerisSnapshotRef:
    return EphemerisSnapshotRef(
        snapshot_id=EphemerisSnapshotId(f"eph_{index}"),
        source=(
            EphemerisSource.SPACE_TRACK
            if index % 2 == 0
            else EphemerisSource.HUGGING_FACE
        ),
        raw_digest=digest(f"ephemeris-raw-{index}"),
        normalized_digest=digest(f"ephemeris-normalized-{index}"),
    )


def request(
    snapshot: FeatureDatasetSnapshot,
    config: ReceiverQualityAggregateConfig,
    hardware_refs: tuple[HardwareMetadataSnapshotRef, ...],
    ephemeris_refs: tuple[EphemerisSnapshotRef, ...] = (),
) -> ModelAnalysisRequest:
    return ModelAnalysisRequest(
        schema=SchemaRef(ModelAnalysisRequest.SCHEMA_ID),
        dataset_snapshot_ref=FeatureDatasetSnapshotRef(
            snapshot.snapshot_id, snapshot.membership_digest
        ),
        hardware_metadata_snapshot_refs=hardware_refs,
        ephemeris_snapshot_refs=ephemeris_refs,
        model_config_ref=receiver_quality_aggregate_config_ref(config),
        algorithm_ref=receiver_quality_aggregate_algorithm_ref(),
    )


@dataclass
class FakeFeatureSetView:
    ref: FeatureSetRef
    value: FeatureSetBundle
    bundle_calls: int = 0

    def bundle(self) -> FeatureSetBundle:
        self.bundle_calls += 1
        return self.value


class _FeatureContext:
    def __init__(self, view: FakeFeatureSetView) -> None:
        self._view = view

    def __enter__(self) -> FakeFeatureSetView:
        return self._view

    def __exit__(self, *args: object) -> None:
        return None


class FakeFeatureSetReader:
    def __init__(
        self, entries: tuple[tuple[FeatureSetRef, FeatureSetBundle], ...]
    ) -> None:
        self._entries = {ref.feature_set_id: (ref, bundle) for ref, bundle in entries}
        self.calls: list[FeatureSetRef] = []
        self.returned_ref_override: FeatureSetRef | None = None
        self.views: list[FakeFeatureSetView] = []

    def open(self, ref: FeatureSetRef) -> _FeatureContext:
        self.calls.append(ref)
        stored_ref, bundle = self._entries[ref.feature_set_id]
        view = FakeFeatureSetView(self.returned_ref_override or stored_ref, bundle)
        self.views.append(view)
        return _FeatureContext(view)


class FakeHardwareReader:
    def __init__(
        self,
        entries: tuple[
            tuple[HardwareMetadataSnapshotRef, HardwareMetadataSnapshot], ...
        ],
    ) -> None:
        self._entries = {ref.snapshot_id: snapshot for ref, snapshot in entries}
        self.calls: list[HardwareMetadataSnapshotRef] = []

    def get(self, ref: HardwareMetadataSnapshotRef) -> HardwareMetadataSnapshot:
        self.calls.append(ref)
        return self._entries[ref.snapshot_id]


@dataclass
class FakeEphemerisView:
    ref: EphemerisSnapshotRef
    normalized_calls: int = 0

    def normalized_bytes(self) -> bytes:
        self.normalized_calls += 1
        return b"unused-normalized-ephemeris"


class _EphemerisContext:
    def __init__(self, view: FakeEphemerisView) -> None:
        self._view = view

    def __enter__(self) -> FakeEphemerisView:
        return self._view

    def __exit__(self, *args: object) -> None:
        return None


class FakeEphemerisReader:
    def __init__(self, refs: tuple[EphemerisSnapshotRef, ...]) -> None:
        self._refs = {ref.snapshot_id: ref for ref in refs}
        self.calls: list[EphemerisSnapshotRef] = []
        self.views: list[FakeEphemerisView] = []
        self.returned_ref_override: EphemerisSnapshotRef | None = None

    def open(self, ref: EphemerisSnapshotRef) -> _EphemerisContext:
        self.calls.append(ref)
        view = FakeEphemerisView(
            self.returned_ref_override or self._refs[ref.snapshot_id]
        )
        self.views.append(view)
        return _EphemerisContext(view)
