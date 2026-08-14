from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace

import pytest

from leo_flow.analysis.model import (
    ExactTrackingSourceInputs,
    ExperimentalFixedNoradTrackingConfig,
    ExperimentalFixedNoradTrackingModel,
    ExtractedTrackingInput,
    RfFeatureSelector,
    TrackingInputExtractionError,
    UnsupportedV01FeatureTrackingExtractor,
    experimental_tracking_algorithm_ref,
    experimental_tracking_config_ref,
)
from leo_flow.analysis.model.persistence import model_snapshot_projection
from leo_flow.analysis.orbit import (
    AssociationPolicy,
    DeterministicOrbitSimulator,
    EphemerisLinkEvidence,
    PropagatedState,
    PropagationSpecification,
    ReceiverRfCalibration,
    RfMeasurement,
    SatelliteCarrierHypothesis,
    StationGeometrySnapshot,
)
from leo_flow.analysis.tracking import TrackingSpecification
from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
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
    canonical_digest,
)
from leo_flow.contracts.ephemeris import (
    EphemerisSelectionPolicy,
    EphemerisSnapshotRef,
    EphemerisSource,
    RecordingInterval,
)
from leo_flow.contracts.features import (
    Covariance,
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
from leo_flow.deployments.offline_analysis_v1 import (
    AlgorithmKey,
    ExactModelFitterRegistry,
)
from leo_flow.hardware.codec import encode_hardware_snapshot
from tests.model_analysis.fakes import FakeFeatureSetReader, execution_context

NORAD_ID = 42_001
NORMALIZED = b'{"exact":"normalized-tle-catalog"}'


def _digest(label: str) -> Digest:
    return Digest.sha256(label.encode())


def _artifact(label: str) -> ArtifactRef:
    return ArtifactRef(label, _digest(label))


def _station() -> StationGeometrySnapshot:
    identity = {
        "station_id": "station_tracking_fit",
        "frame": "ITRF",
        "position_m": (6_378_137.0, 0.0, 0.0),
    }
    return StationGeometrySnapshot(
        StationId(identity["station_id"]),
        identity["frame"],
        identity["position_m"],
        canonical_digest(identity),
    )


def _config() -> ExperimentalFixedNoradTrackingConfig:
    return ExperimentalFixedNoradTrackingConfig(
        _artifact("typed-rf-evidence-extractor-v0.1"),
        RfFeatureSelector("rf-carrier", "0.1.0", "rf-frequency-drift"),
        _station(),
        PropagationSpecification(
            _artifact("propagator-v1"),
            _artifact("gravity-v1"),
            _artifact("time-v1"),
            _artifact("eop-v1"),
            _artifact("error-v1"),
        ),
        (SatelliteCarrierHypothesis(NORAD_ID, 1_000_000_000.0, 1.0),),
        AssociationPolicy(_artifact("association-v1"), 5.0, 100.0, 0.1),
        TrackingSpecification(
            _artifact("tracking-v1"),
            NORAD_ID,
            (0.0, 0.0),
            ((100.0, 0.0), (0.0, 1.0)),
            0.001,
            100.0,
            30.0,
        ),
    )


def _feature(index: int) -> tuple[FeatureSetRef, FeatureSetBundle]:
    recording_id = RecordingId(f"rec_tracking_fit_{index}")
    feature_set_id = FeatureSetId(f"fset_tracking_fit_{index}")
    run_id = AnalysisRunId(f"arun_tracking_fit_{index}")
    ref = FeatureSetRef(
        feature_set_id,
        run_id,
        ObjectRef(
            _digest(f"feature-bundle-{index}"),
            100,
            "application/json",
            "feature-set-v0.1",
            f"memory://feature/{index}",
        ),
    )
    midpoint = UtcNs(10_000_000_000 + index * 5_000_000_000)
    observation = FeatureObservation(
        FeatureId(f"feature_tracking_fit_{index}"),
        recording_id,
        SegmentId(f"seg_tracking_fit_{index}"),
        "rf-carrier",
        "0.1.0",
        0,
        64,
        64,
        midpoint,
        "rf-frequency-drift",
        1.0,
        "detector-score",
        receiver_chain_id=ReceiverChainId("rx_tracking_fit"),
        frequency_hz=1_000_000_002.0 + index,
        drift_hz_s=0.1,
        covariance=Covariance(
            ("frequency_hz", "drift_hz_s"),
            ("Hz", "Hz/s"),
            ((4.0, 0.0), (0.0, 0.04)),
        ),
    )
    provenance = Provenance(
        "fixture",
        "0.1.0",
        "fixture-git",
        _digest("feature-env"),
        _digest("feature-config"),
        (_digest(f"recording-{index}"),),
        (_digest("recording-analyzer"),),
        UtcNs(1),
        UtcNs(2),
        "test",
    )
    return ref, FeatureSetBundle(
        SchemaRef(FeatureSetBundle.SCHEMA_ID),
        feature_set_id,
        run_id,
        recording_id,
        _digest(f"recording-identity-{index}"),
        provenance,
        (observation,),
        (),
    )


def _dataset(
    entries: tuple[tuple[FeatureSetRef, FeatureSetBundle], ...],
) -> FeatureDatasetSnapshot:
    refs = tuple(ref for ref, _ in entries)
    return FeatureDatasetSnapshot(
        SchemaRef(FeatureDatasetSnapshot.SCHEMA_ID),
        DatasetSnapshotId("dataset_tracking_fit"),
        refs,
        "fixture:explicit",
        UtcNs(30_000_000_000),
        feature_dataset_membership_digest(refs),
    )


def _hardware() -> tuple[HardwareMetadataSnapshotRef, HardwareMetadataSnapshot]:
    snapshot = HardwareMetadataSnapshot(
        SchemaRef(HardwareMetadataSnapshot.SCHEMA_ID),
        HardwareSnapshotId("hw_tracking_fit"),
        StationId("station_tracking_fit"),
        (RadioId("radio_tracking_fit"),),
        (
            ReceiverChainMetadata(
                ReceiverChainId("rx_tracking_fit"),
                RadioId("radio_tracking_fit"),
                0,
                "lnb-tracking-fit",
                None,
                None,
                UtcNs(0),
                None,
            ),
        ),
    )
    return HardwareMetadataSnapshotRef(
        snapshot.snapshot_id, Digest.sha256(encode_hardware_snapshot(snapshot))
    ), snapshot


def _ephemeris() -> EphemerisSnapshotRef:
    return EphemerisSnapshotRef(
        EphemerisSnapshotId("eph_tracking_fit"),
        EphemerisSource.SPACE_TRACK,
        _digest("raw-tle"),
        Digest.sha256(NORMALIZED),
    )


@dataclass
class _EphemerisView:
    ref: EphemerisSnapshotRef

    def normalized_bytes(self) -> bytes:
        return NORMALIZED


class _Context(AbstractContextManager[_EphemerisView]):
    def __init__(self, view: _EphemerisView) -> None:
        self.view = view

    def __enter__(self) -> _EphemerisView:
        return self.view

    def __exit__(self, *args: object) -> None:
        return None


class _EphemerisReader:
    def __init__(self, ref: EphemerisSnapshotRef) -> None:
        self.ref = ref
        self.calls: list[EphemerisSnapshotRef] = []

    def open(self, ref: EphemerisSnapshotRef) -> _Context:
        self.calls.append(ref)
        return _Context(_EphemerisView(self.ref))


class _HardwareReader:
    def __init__(
        self, ref: HardwareMetadataSnapshotRef, value: HardwareMetadataSnapshot
    ) -> None:
        self.ref = ref
        self.value = value
        self.calls: list[HardwareMetadataSnapshotRef] = []

    def get(self, ref: HardwareMetadataSnapshotRef) -> HardwareMetadataSnapshot:
        self.calls.append(ref)
        return self.value


class _EvidenceExtractor:
    @property
    def artifact_ref(self) -> ArtifactRef:
        return _artifact("typed-rf-evidence-extractor-v0.1")

    def extract(
        self,
        inputs: ExactTrackingSourceInputs,
        config: ExperimentalFixedNoradTrackingConfig,
    ) -> tuple[ExtractedTrackingInput, ...]:
        ephemeris = inputs.ephemerides[0]
        hardware_ref, hardware = inputs.hardware[0]
        values = []
        for feature_ref, bundle in inputs.feature_sets:
            observation = bundle.observations[0]
            assert observation.receiver_chain_id is not None
            assert observation.frequency_hz is not None
            assert observation.drift_hz_s is not None
            interval = RecordingInterval(
                UtcNs(int(observation.midpoint_utc_ns) - 1),
                UtcNs(int(observation.midpoint_utc_ns) + 1),
            )
            selection_ref = _artifact("available-then-v1")
            identity = {
                "recording_identity_digest": str(
                    bundle.input_recording_identity_digest
                ),
                "recording_interval": interval,
                "source": ephemeris.source.value,
                "scope": "tracking-fit",
                "policy": EphemerisSelectionPolicy.AVAILABLE_THEN.value,
                "policy_ref": selection_ref,
                "as_of_utc_ns": UtcNs(20_000_000_000),
                "snapshot_ref": ephemeris,
            }
            link_digest = canonical_digest(identity)
            link = EphemerisLinkEvidence(
                ArtifactRef(
                    f"ephlink_{link_digest.value[:32]}",
                    link_digest,
                    SchemaRef("org.leo-flow.recording-ephemeris-link"),
                ),
                bundle.recording_id,
                bundle.input_recording_identity_digest,
                interval,
                ephemeris.source,
                "tracking-fit",
                EphemerisSelectionPolicy.AVAILABLE_THEN,
                selection_ref,
                UtcNs(20_000_000_000),
                ephemeris,
            )
            measurement = RfMeasurement(
                feature_ref,
                observation.feature_id,
                bundle.recording_id,
                observation.receiver_chain_id,
                observation.midpoint_utc_ns,
                observation.frequency_hz,
                observation.drift_hz_s,
                4.0,
                0.04,
            )
            values.append(
                ExtractedTrackingInput(
                    measurement,
                    link,
                    ReceiverRfCalibration(
                        observation.receiver_chain_id,
                        hardware_ref,
                        hardware.station_id,
                        0.0,
                        0.0,
                        1.0,
                        0.01,
                    ),
                    2.0,
                    0.01,
                )
            )
        return tuple(values)


def _outside_recording_interval(
    item: ExtractedTrackingInput,
) -> ExtractedTrackingInput:
    link = item.ephemeris_link
    midpoint = item.measurement.midpoint_utc_ns
    interval = RecordingInterval(UtcNs(int(midpoint) + 1), UtcNs(int(midpoint) + 2))
    identity = {
        "recording_identity_digest": str(link.recording_identity_digest),
        "recording_interval": interval,
        "source": link.source.value,
        "scope": link.scope,
        "policy": link.selection_policy.value,
        "policy_ref": link.selection_policy_ref,
        "as_of_utc_ns": link.as_of_utc_ns,
        "snapshot_ref": link.snapshot_ref,
    }
    digest = canonical_digest(identity)
    changed = EphemerisLinkEvidence(
        ArtifactRef(
            f"ephlink_{digest.value[:32]}",
            digest,
            SchemaRef("org.leo-flow.recording-ephemeris-link"),
        ),
        link.recording_id,
        link.recording_identity_digest,
        interval,
        link.source,
        link.scope,
        link.selection_policy,
        link.selection_policy_ref,
        link.as_of_utc_ns,
        link.snapshot_ref,
    )
    return replace(item, ephemeris_link=changed)


@dataclass
class _Fixture:
    config: ExperimentalFixedNoradTrackingConfig
    dataset: FeatureDatasetSnapshot
    feature_entries: tuple[tuple[FeatureSetRef, FeatureSetBundle], ...]
    features: FakeFeatureSetReader
    ephemeris_ref: EphemerisSnapshotRef
    ephemerides: _EphemerisReader
    hardware_ref: HardwareMetadataSnapshotRef
    hardware: _HardwareReader

    def request(self) -> ModelAnalysisRequest:
        return ModelAnalysisRequest(
            SchemaRef(ModelAnalysisRequest.SCHEMA_ID),
            FeatureDatasetSnapshotRef(
                self.dataset.snapshot_id, self.dataset.membership_digest
            ),
            (self.hardware_ref,),
            (self.ephemeris_ref,),
            experimental_tracking_config_ref(self.config),
            experimental_tracking_algorithm_ref(),
        )


def _fixture() -> _Fixture:
    entries = (_feature(0), _feature(1))
    hardware_ref, hardware = _hardware()
    ephemeris = _ephemeris()
    return _Fixture(
        _config(),
        _dataset(entries),
        entries,
        FakeFeatureSetReader(entries),
        ephemeris,
        _EphemerisReader(ephemeris),
        hardware_ref,
        _HardwareReader(hardware_ref, hardware),
    )


def test_opt_in_fitter_runs_behind_exact_offline_registry() -> None:
    fixture = _fixture()
    states = tuple(
        PropagatedState(
            NORAD_ID,
            bundle.observations[0].midpoint_utc_ns,
            0.0,
            0.0,
            30.0,
        )
        for _, bundle in fixture.feature_entries
    )
    key = AlgorithmKey(
        experimental_tracking_algorithm_ref(),
        experimental_tracking_config_ref(fixture.config),
    )
    registry = ExactModelFitterRegistry(
        {
            key: lambda dataset: ExperimentalFixedNoradTrackingModel(
                dataset,
                fixture.config,
                execution_context(),
                _EvidenceExtractor(),
                lambda reader: DeterministicOrbitSimulator(states),
            )
        }
    )

    bundle = registry(fixture.dataset).fit(
        fixture.request(), fixture.features, fixture.ephemerides, fixture.hardware
    )

    assert len(bundle.parameters) == 2
    assert all(
        item.parameter_id == "experimental-fixed-norad-residual"
        for item in bundle.parameters
    )
    assert "experimental:not-satellite-truth" in bundle.warnings
    assert (
        fixture.request().algorithm_ref.digest in bundle.provenance.dependency_digests
    )
    assert (
        fixture.request().model_config_ref.digest
        == bundle.provenance.normalized_config_digest
    )
    model_snapshot_projection(fixture.request(), bundle)
    assert fixture.ephemerides.calls == [fixture.ephemeris_ref]
    assert fixture.hardware.calls == [fixture.hardware_ref]


def test_v01_boundary_fails_closed_before_association() -> None:
    fixture = _fixture()
    model = ExperimentalFixedNoradTrackingModel(
        fixture.dataset,
        fixture.config,
        execution_context(),
        UnsupportedV01FeatureTrackingExtractor(fixture.config.extractor_ref),
        lambda reader: pytest.fail("propagator must not be created"),
    )

    with pytest.raises(
        TrackingInputExtractionError, match="lacks authoritative recording interval"
    ):
        model.fit(
            fixture.request(), fixture.features, fixture.ephemerides, fixture.hardware
        )


def test_extractor_cannot_change_feature_measurement_or_covariance() -> None:
    fixture = _fixture()

    class _SubstitutingExtractor(_EvidenceExtractor):
        def extract(
            self,
            inputs: ExactTrackingSourceInputs,
            config: ExperimentalFixedNoradTrackingConfig,
        ) -> tuple[ExtractedTrackingInput, ...]:
            values = super().extract(inputs, config)
            return (
                replace(
                    values[0],
                    measurement=replace(values[0].measurement, frequency_hz=0.0),
                ),
            ) + values[1:]

    model = ExperimentalFixedNoradTrackingModel(
        fixture.dataset,
        fixture.config,
        execution_context(),
        _SubstitutingExtractor(),
        lambda reader: pytest.fail("propagator must not be created"),
    )

    with pytest.raises(TrackingInputExtractionError, match="differs from feature"):
        model.fit(
            fixture.request(), fixture.features, fixture.ephemerides, fixture.hardware
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda item: replace(
                item,
                ephemeris_link=replace(
                    item.ephemeris_link,
                    recording_id=RecordingId("rec_substituted"),
                ),
            ),
            "ephemeris link recording ID differs",
        ),
        (
            _outside_recording_interval,
            "outside ephemeris-linked recording interval",
        ),
        (
            lambda item: replace(
                item,
                calibration=replace(
                    item.calibration,
                    receiver_chain_id=ReceiverChainId("rx_substituted"),
                ),
            ),
            "calibration and measurement chains differ",
        ),
    ],
)
def test_link_and_calibration_mismatch_fail_before_propagation(
    mutation: Callable[[ExtractedTrackingInput], ExtractedTrackingInput], message: str
) -> None:
    fixture = _fixture()

    class _MismatchingExtractor(_EvidenceExtractor):
        def extract(
            self,
            inputs: ExactTrackingSourceInputs,
            config: ExperimentalFixedNoradTrackingConfig,
        ) -> tuple[ExtractedTrackingInput, ...]:
            values = super().extract(inputs, config)
            changed = mutation(values[0])
            return (changed,) + values[1:]

    model = ExperimentalFixedNoradTrackingModel(
        fixture.dataset,
        fixture.config,
        execution_context(),
        _MismatchingExtractor(),
        lambda reader: pytest.fail("propagator must not be created"),
    )

    with pytest.raises(TrackingInputExtractionError, match=message):
        model.fit(
            fixture.request(), fixture.features, fixture.ephemerides, fixture.hardware
        )


def test_config_identity_closes_over_carriers_station_policies_and_extractor() -> None:
    config = _config()
    original = experimental_tracking_config_ref(config)

    variants = (
        replace(config, extractor_ref=_artifact("another-extractor")),
        replace(
            config,
            carriers=(SatelliteCarrierHypothesis(NORAD_ID, 1_000_000_001.0, 1.0),),
        ),
        replace(
            config,
            association=replace(
                config.association, maximum_normalized_squared_residual=99.0
            ),
        ),
        replace(
            config,
            tracking=replace(config.tracking, maximum_gap_s=31.0),
        ),
    )

    assert all(experimental_tracking_config_ref(item) != original for item in variants)
