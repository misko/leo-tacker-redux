"""Opt-in second-stage model fitter for experimental fixed-NORAD tracking.

The frozen model port does not expose recording intervals, authoritative
recording-to-ephemeris links, or RF calibration.  Those values are scientific
inputs, so this module accepts them only through a deliberately injected
extractor operating on already identity-verified model inputs.  There is no
default extractor and no filename, latest-row, or network fallback.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from leo_flow.analysis.orbit import (
    AssociationPolicy,
    EphemerisLinkEvidence,
    OrbitPropagator,
    PropagationSpecification,
    ReceiverRfCalibration,
    RfAssociationRequest,
    RfMeasurement,
    SatelliteCarrierHypothesis,
    StationGeometrySnapshot,
    associate_rf_measurement,
)
from leo_flow.analysis.tracking import (
    AssociatedTrackingObservation,
    TrackingReport,
    TrackingSpecification,
    track_associated_observations,
)
from leo_flow.contracts._validation import require_nonnegative
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    ModelRunId,
    ModelSnapshotId,
    Provenance,
    SchemaRef,
    canonical_digest,
)
from leo_flow.contracts.ephemeris import EphemerisSnapshotRef
from leo_flow.contracts.features import (
    Covariance,
    FeatureObservation,
    FeatureSetBundle,
    FeatureSetRef,
)
from leo_flow.contracts.hardware import (
    HardwareMetadataSnapshot,
    HardwareMetadataSnapshotRef,
)
from leo_flow.contracts.model import (
    FeatureDatasetSnapshot,
    FeatureDatasetSnapshotRef,
    ModelAnalysisRequest,
    ModelSnapshotBundle,
    ParameterEstimate,
)
from leo_flow.contracts.ports import (
    EphemerisReader,
    FeatureSetReader,
    HardwareMetadataReader,
)
from leo_flow.hardware.codec import encode_hardware_snapshot

from .api import ModelConfigurationError, ModelExecutionContext, ModelInputError

ALGORITHM_ID = "experimental-fixed-norad-residual-tracking"
ALGORITHM_VERSION = "0.1.0"
CONFIG_SCHEMA_ID = "org.leo-flow.experimental-fixed-norad-tracking-config"
FREQUENCY_BASIS = "frequency_hz"
DRIFT_BASIS = "drift_hz_s"


class TrackingInputExtractionError(ModelInputError):
    """The exact model inputs cannot produce complete tracking evidence."""


@dataclass(frozen=True)
class RfFeatureSelector:
    method_id: str
    method_version: str
    feature_kind: str

    def __post_init__(self) -> None:
        if not self.method_id or not self.method_version or not self.feature_kind:
            raise ValueError("RF feature selector values must be non-empty")


@dataclass(frozen=True)
class ExperimentalFixedNoradTrackingConfig:
    """All immutable scientific choices for one opt-in experiment."""

    extractor_ref: ArtifactRef
    selector: RfFeatureSelector
    station: StationGeometrySnapshot
    propagation: PropagationSpecification
    carriers: tuple[SatelliteCarrierHypothesis, ...]
    association: AssociationPolicy
    tracking: TrackingSpecification

    def __post_init__(self) -> None:
        if not self.carriers:
            raise ValueError("tracking config requires carrier hypotheses")
        if len({item.norad_id for item in self.carriers}) != len(self.carriers):
            raise ValueError("carrier hypotheses must have unique NORAD IDs")
        if self.tracking.expected_norad_id not in {
            item.norad_id for item in self.carriers
        }:
            raise ValueError("fixed tracking NORAD ID has no carrier hypothesis")


@dataclass(frozen=True)
class ExactTrackingSourceInputs:
    """Materialized values from only the exact refs named by the request."""

    dataset: FeatureDatasetSnapshot
    feature_sets: tuple[tuple[FeatureSetRef, FeatureSetBundle], ...]
    ephemerides: tuple[EphemerisSnapshotRef, ...]
    hardware: tuple[tuple[HardwareMetadataSnapshotRef, HardwareMetadataSnapshot], ...]


@dataclass(frozen=True)
class ExtractedTrackingInput:
    """Evidence that the v0.1 FeatureSet contract cannot itself represent."""

    measurement: RfMeasurement
    ephemeris_link: EphemerisLinkEvidence
    calibration: ReceiverRfCalibration
    prediction_frequency_variance_hz2: float
    prediction_drift_variance_hz2_s2: float

    def __post_init__(self) -> None:
        require_nonnegative(
            self.prediction_frequency_variance_hz2,
            "prediction_frequency_variance_hz2",
        )
        require_nonnegative(
            self.prediction_drift_variance_hz2_s2,
            "prediction_drift_variance_hz2_s2",
        )


class TrackingInputExtractor(Protocol):
    """Deployment-owned, content-identified extraction from exact values only."""

    @property
    def artifact_ref(self) -> ArtifactRef: ...

    def extract(
        self,
        inputs: ExactTrackingSourceInputs,
        config: ExperimentalFixedNoradTrackingConfig,
    ) -> tuple[ExtractedTrackingInput, ...]: ...


class UnsupportedV01FeatureTrackingExtractor:
    """Fail closed until recording analysis publishes complete typed evidence."""

    def __init__(self, artifact_ref: ArtifactRef) -> None:
        self._artifact_ref = artifact_ref

    @property
    def artifact_ref(self) -> ArtifactRef:
        return self._artifact_ref

    def extract(
        self,
        inputs: ExactTrackingSourceInputs,
        config: ExperimentalFixedNoradTrackingConfig,
    ) -> tuple[ExtractedTrackingInput, ...]:
        del inputs, config
        raise TrackingInputExtractionError(
            "FeatureSet v0.1 has RF values but lacks authoritative recording "
            "interval, ephemeris-link evidence, and receiver RF calibration; "
            "inject a content-identified extractor for an analyzer output that "
            "publishes those fields"
        )


OrbitPropagatorFactory = Callable[[EphemerisReader], OrbitPropagator]


def experimental_tracking_algorithm_ref() -> ArtifactRef:
    return ArtifactRef(
        f"{ALGORITHM_ID}-v0.1",
        canonical_digest(
            {
                "algorithm_id": ALGORITHM_ID,
                "algorithm_version": ALGORITHM_VERSION,
                "association": "offline-rf-association-v0.1",
                "tracker": "fixed-norad-residual-filter-rts-v0.1",
                "input_policy": "exact-reader-refs-and-injected-extractor-only",
                "output_label": "experimental-not-satellite-truth",
            }
        ),
        SchemaRef("org.leo-flow.model-algorithm"),
    )


def experimental_tracking_config_ref(
    config: ExperimentalFixedNoradTrackingConfig,
) -> ArtifactRef:
    return ArtifactRef(
        f"{ALGORITHM_ID}-config-v0.1",
        canonical_digest(config),
        SchemaRef(CONFIG_SCHEMA_ID),
    )


class ExperimentalFixedNoradTrackingModel:
    """Associate exact RF observations, then track one fixed NORAD residual."""

    def __init__(
        self,
        dataset: FeatureDatasetSnapshot,
        config: ExperimentalFixedNoradTrackingConfig,
        execution: ModelExecutionContext,
        extractor: TrackingInputExtractor,
        propagator_factory: OrbitPropagatorFactory,
    ) -> None:
        self._dataset = dataset
        self._config = config
        self._execution = execution
        self._extractor = extractor
        self._propagator_factory = propagator_factory
        if extractor.artifact_ref != config.extractor_ref:
            raise ModelConfigurationError(
                "injected extractor does not match content-addressed config"
            )

    def fit(
        self,
        request: ModelAnalysisRequest,
        features: FeatureSetReader,
        ephemerides: EphemerisReader,
        hardware: HardwareMetadataReader,
    ) -> ModelSnapshotBundle:
        hardware_refs, ephemeris_refs = self._validate_request(request)
        exact = ExactTrackingSourceInputs(
            self._dataset,
            tuple(
                (ref, self._open_feature(ref, features))
                for ref in self._dataset.ordered_feature_set_refs
            ),
            self._verify_ephemerides(ephemeris_refs, ephemerides),
            self._load_hardware(hardware_refs, hardware),
        )
        extracted = self._extractor.extract(exact, self._config)
        self._validate_extracted(exact, extracted)
        propagator = self._propagator_factory(ephemerides)
        associated = tuple(
            self._associate(item, propagator)
            for item in sorted(
                extracted,
                key=lambda value: (
                    int(value.measurement.midpoint_utc_ns),
                    str(value.measurement.feature_set_ref.feature_set_id),
                    str(value.measurement.feature_id),
                ),
            )
        )
        report = track_associated_observations(associated, self._config.tracking)
        return self._bundle(request, hardware_refs, ephemeris_refs, report)

    def _associate(
        self, item: ExtractedTrackingInput, propagator: OrbitPropagator
    ) -> AssociatedTrackingObservation:
        association_request = RfAssociationRequest(
            item.ephemeris_link,
            self._config.station,
            self._config.propagation,
            item.measurement,
            item.calibration,
            self._config.carriers,
            self._config.association,
        )
        return AssociatedTrackingObservation(
            associate_rf_measurement(association_request, propagator),
            association_request,
            item.prediction_frequency_variance_hz2,
            item.prediction_drift_variance_hz2_s2,
        )

    def _validate_request(
        self, request: ModelAnalysisRequest
    ) -> tuple[
        tuple[HardwareMetadataSnapshotRef, ...], tuple[EphemerisSnapshotRef, ...]
    ]:
        expected_dataset = FeatureDatasetSnapshotRef(
            self._dataset.snapshot_id, self._dataset.membership_digest
        )
        if request.dataset_snapshot_ref != expected_dataset:
            raise ModelConfigurationError(
                "request dataset differs from injected snapshot"
            )
        if request.algorithm_ref != experimental_tracking_algorithm_ref():
            raise ModelConfigurationError("request algorithm does not identify tracker")
        if request.model_config_ref != experimental_tracking_config_ref(self._config):
            raise ModelConfigurationError("request config differs from injected config")
        hardware_refs = tuple(
            sorted(
                request.hardware_metadata_snapshot_refs,
                key=lambda ref: (str(ref.snapshot_id), str(ref.digest)),
            )
        )
        ephemeris_refs = tuple(
            sorted(
                request.ephemeris_snapshot_refs,
                key=lambda ref: (ref.source.value, str(ref.snapshot_id)),
            )
        )
        if len({ref.snapshot_id for ref in hardware_refs}) != len(hardware_refs):
            raise ModelConfigurationError("duplicate hardware snapshot IDs")
        if len({ref.snapshot_id for ref in ephemeris_refs}) != len(ephemeris_refs):
            raise ModelConfigurationError("duplicate ephemeris snapshot IDs")
        if not ephemeris_refs:
            raise ModelConfigurationError("tracking requires exact ephemeris refs")
        return hardware_refs, ephemeris_refs

    @staticmethod
    def _open_feature(ref: FeatureSetRef, reader: FeatureSetReader) -> FeatureSetBundle:
        with reader.open(ref) as view:
            if view.ref != ref:
                raise ModelInputError("feature reader substituted exact reference")
            bundle = view.bundle()
        if (
            bundle.feature_set_id != ref.feature_set_id
            or bundle.analysis_run_id != ref.analysis_run_id
        ):
            raise ModelInputError("feature bundle differs from exact reference")
        return bundle

    @staticmethod
    def _verify_ephemerides(
        refs: tuple[EphemerisSnapshotRef, ...], reader: EphemerisReader
    ) -> tuple[EphemerisSnapshotRef, ...]:
        for ref in refs:
            with reader.open(ref) as view:
                if view.ref != ref:
                    raise ModelInputError(
                        "ephemeris reader substituted exact reference"
                    )
        return refs

    @staticmethod
    def _load_hardware(
        refs: tuple[HardwareMetadataSnapshotRef, ...], reader: HardwareMetadataReader
    ) -> tuple[tuple[HardwareMetadataSnapshotRef, HardwareMetadataSnapshot], ...]:
        values = []
        for ref in refs:
            snapshot = reader.get(ref)
            if snapshot.snapshot_id != ref.snapshot_id:
                raise ModelInputError("hardware reader substituted exact reference")
            if Digest.sha256(encode_hardware_snapshot(snapshot)) != ref.digest:
                raise ModelInputError("hardware snapshot digest differs")
            values.append((ref, snapshot))
        return tuple(values)

    def _validate_extracted(
        self,
        exact: ExactTrackingSourceInputs,
        extracted: tuple[ExtractedTrackingInput, ...],
    ) -> None:
        eligible: dict[
            tuple[FeatureSetRef, object], tuple[FeatureSetBundle, FeatureObservation]
        ] = {}
        for ref, bundle in exact.feature_sets:
            for observation in bundle.observations:
                if (
                    observation.method_id == self._config.selector.method_id
                    and observation.method_version
                    == self._config.selector.method_version
                    and observation.feature_kind == self._config.selector.feature_kind
                ):
                    eligible[(ref, observation.feature_id)] = (bundle, observation)
        if not eligible:
            raise TrackingInputExtractionError(
                "no RF observations match exact selector"
            )
        seen: set[tuple[FeatureSetRef, object]] = set()
        ephemeris_refs = set(exact.ephemerides)
        hardware_by_ref = dict(exact.hardware)
        for item in extracted:
            key = (item.measurement.feature_set_ref, item.measurement.feature_id)
            if key in seen or key not in eligible:
                raise TrackingInputExtractionError(
                    "extractor returned duplicate or unselected feature"
                )
            seen.add(key)
            bundle, observation = eligible[key]
            self._verify_measurement(bundle, observation, item.measurement)
            if item.ephemeris_link.snapshot_ref not in ephemeris_refs:
                raise TrackingInputExtractionError(
                    "extractor used unrequested ephemeris"
                )
            if (
                item.ephemeris_link.recording_identity_digest
                != bundle.input_recording_identity_digest
            ):
                raise TrackingInputExtractionError(
                    "ephemeris link recording identity differs"
                )
            if item.ephemeris_link.recording_id != item.measurement.recording_id:
                raise TrackingInputExtractionError(
                    "ephemeris link recording ID differs"
                )
            if not (
                item.ephemeris_link.recording_interval.started_utc_ns
                <= item.measurement.midpoint_utc_ns
                <= item.ephemeris_link.recording_interval.finished_utc_ns
            ):
                raise TrackingInputExtractionError(
                    "RF measurement lies outside ephemeris-linked recording interval"
                )
            if item.calibration.receiver_chain_id != item.measurement.receiver_chain_id:
                raise TrackingInputExtractionError(
                    "RF calibration and measurement chains differ"
                )
            if item.calibration.station_id != self._config.station.station_id:
                raise TrackingInputExtractionError(
                    "RF calibration and station identities differ"
                )
            snapshot = hardware_by_ref.get(item.calibration.hardware_snapshot_ref)
            if snapshot is None:
                raise TrackingInputExtractionError(
                    "extractor used unrequested hardware"
                )
            if snapshot.station_id != self._config.station.station_id:
                raise TrackingInputExtractionError(
                    "hardware and station identities differ"
                )
            effective = tuple(
                chain
                for chain in snapshot.receiver_chains
                if chain.receiver_chain_id == item.measurement.receiver_chain_id
                and chain.valid_from_utc_ns <= item.measurement.midpoint_utc_ns
                and (
                    chain.valid_until_utc_ns is None
                    or item.measurement.midpoint_utc_ns < chain.valid_until_utc_ns
                )
            )
            if len(effective) != 1:
                raise TrackingInputExtractionError(
                    "RF measurement has no unique effective hardware chain"
                )
        if seen != set(eligible):
            raise TrackingInputExtractionError(
                "extractor omitted selected RF observations"
            )

    @staticmethod
    def _verify_measurement(
        bundle: FeatureSetBundle,
        observation: FeatureObservation,
        measurement: RfMeasurement,
    ) -> None:
        if (
            measurement.recording_id != bundle.recording_id
            or measurement.receiver_chain_id != observation.receiver_chain_id
            or measurement.midpoint_utc_ns != observation.midpoint_utc_ns
            or measurement.frequency_hz != observation.frequency_hz
            or measurement.drift_hz_s != observation.drift_hz_s
        ):
            raise TrackingInputExtractionError(
                "RF measurement differs from feature observation"
            )
        covariance = observation.covariance
        if covariance is None:
            raise TrackingInputExtractionError("RF observation lacks covariance")
        expected = _rf_variances(covariance)
        if (
            measurement.frequency_variance_hz2,
            measurement.drift_variance_hz2_s2,
        ) != expected:
            raise TrackingInputExtractionError(
                "RF measurement variance differs from covariance"
            )

    def _bundle(
        self,
        request: ModelAnalysisRequest,
        hardware_refs: tuple[HardwareMetadataSnapshotRef, ...],
        ephemeris_refs: tuple[EphemerisSnapshotRef, ...],
        report: TrackingReport,
    ) -> ModelSnapshotBundle:
        parameters = []
        for segment in report.segments:
            for estimate in segment.estimates:
                state = estimate.smoothed_state or estimate.filtered_state
                covariance = (
                    estimate.smoothed_covariance or estimate.filtered_covariance
                )
                parameters.append(
                    ParameterEstimate(
                        "experimental-fixed-norad-residual",
                        f"{estimate.recording_id}:{int(estimate.utc_ns)}",
                        state,
                        estimate.state_basis,
                        ("Hz", "Hz/s"),
                        Covariance(estimate.state_basis, ("Hz", "Hz/s"), covariance),
                    )
                )
        hardware_digests = tuple(ref.digest for ref in hardware_refs)
        ephemeris_digests = tuple(ref.normalized_digest for ref in ephemeris_refs)
        feature_digests = tuple(
            ref.bundle_ref.digest for ref in self._dataset.ordered_feature_set_refs
        )
        identity = {
            "dataset": self._dataset.membership_digest,
            "algorithm_ref": request.algorithm_ref,
            "config_ref": request.model_config_ref,
            "extractor_ref": self._config.extractor_ref,
            "hardware_refs": hardware_refs,
            "ephemeris_refs": ephemeris_refs,
            "report": report,
        }
        model_digest = canonical_digest(identity)
        run_digest = canonical_digest(
            {
                "identity": identity,
                "environment": self._execution.environment_digest,
                "git": self._execution.git_commit,
            }
        )
        provenance = Provenance(
            self._execution.producer_name,
            self._execution.producer_version,
            self._execution.git_commit,
            self._execution.environment_digest,
            request.model_config_ref.digest,
            (self._dataset.membership_digest,)
            + feature_digests
            + (report.input_digest,),
            (request.algorithm_ref.digest,)
            + hardware_digests
            + tuple(
                digest
                for ref in ephemeris_refs
                for digest in (ref.raw_digest, ref.normalized_digest)
            ),
            self._execution.started_utc_ns,
            self._execution.completed_utc_ns,
            self._execution.host_class,
        )
        warnings = [
            "experimental:fixed-norad-residual-tracking",
            "experimental:not-satellite-truth",
            f"experimental:expected-norad:{report.expected_norad_id}",
            f"experimental:segments:{len(report.segments)}",
        ]
        warnings.extend(
            f"experimental:rejected:{item.recording_id}:{item.reason_code}"
            for item in report.rejected
        )
        return ModelSnapshotBundle(
            SchemaRef(ModelSnapshotBundle.SCHEMA_ID),
            ModelSnapshotId(f"model_{model_digest.value[:32]}"),
            ModelRunId(f"mrun_{run_digest.value[:32]}"),
            self._dataset.membership_digest,
            hardware_digests,
            ephemeris_digests,
            provenance,
            tuple(parameters),
            tuple(warnings),
        )


def _rf_variances(covariance: Covariance) -> tuple[float, float]:
    try:
        frequency = covariance.basis.index(FREQUENCY_BASIS)
        drift = covariance.basis.index(DRIFT_BASIS)
    except ValueError as error:
        raise TrackingInputExtractionError(
            "RF covariance must contain frequency_hz and drift_hz_s"
        ) from error
    if covariance.units[frequency] != "Hz" or covariance.units[drift] != "Hz/s":
        raise TrackingInputExtractionError("RF covariance units differ")
    if (
        covariance.values[frequency][drift] != 0.0
        or covariance.values[drift][frequency] != 0.0
    ):
        raise TrackingInputExtractionError("correlated RF covariance is unsupported")
    values = covariance.values[frequency][frequency], covariance.values[drift][drift]
    if values[0] <= 0.0 or values[1] <= 0.0:
        raise TrackingInputExtractionError("RF variances must be positive")
    return values
