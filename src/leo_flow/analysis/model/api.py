"""A small structural aggregate over immutable recording feature sets.

The frozen ``ModelFitter`` port carries only a ``FeatureDatasetSnapshotRef`` and
has no dataset reader.  The composition root must therefore resolve the exact
immutable snapshot first and inject it into this fitter.  ``fit`` verifies that
the request pins that snapshot, then opens only its declared feature-set
membership through ``FeatureSetReader``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    ModelRunId,
    ModelSnapshotId,
    Provenance,
    SchemaRef,
    UtcNs,
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
    HardwareMetadataSnapshotRef,
    ReceiverChainMetadata,
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

ALGORITHM_ID = "receiver-quality-aggregate"
ALGORITHM_VERSION = "0.1.0"
CONFIG_SCHEMA_ID = "org.leo-flow.receiver-quality-aggregate-config"
TARGET_METHOD_ID = "sample-quality"
TARGET_METHOD_VERSION = "0.1.0"
TARGET_FEATURE_KIND = "sample-quality"
TARGET_SCORE_SEMANTICS = "rms-magnitude-counts"
PARAMETER_ID = "receiver-mean-rms-magnitude"
PARAMETER_BASIS = "mean_rms_magnitude_counts"
PARAMETER_UNITS = "ADC-count"


class ModelInputError(ValueError):
    """A pinned reader result does not match the immutable model input."""


class ModelConfigurationError(ValueError):
    """A model request does not identify this algorithm and configuration."""


@dataclass(frozen=True)
class ReceiverQualityAggregateConfig:
    """Scientific choices for the one-parameter-per-receiver aggregate."""

    minimum_feature_sets: int = 2
    score_variance_key: str = "score_variance"
    covariance_floor: float = 1e-12

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_feature_sets, bool)
            or not isinstance(self.minimum_feature_sets, int)
            or self.minimum_feature_sets < 2
        ):
            raise ValueError("minimum_feature_sets must be an integer >= 2")
        if not self.score_variance_key or any(
            character.isspace() for character in self.score_variance_key
        ):
            raise ValueError("score_variance_key must be a non-empty token")
        if (
            isinstance(self.covariance_floor, bool)
            or not isinstance(self.covariance_floor, (int, float))
            or not math.isfinite(self.covariance_floor)
            or self.covariance_floor <= 0.0
        ):
            raise ValueError("covariance_floor must be finite and positive")


@dataclass(frozen=True)
class ModelExecutionContext:
    producer_name: str
    producer_version: str
    git_commit: str
    environment_digest: Digest
    started_utc_ns: UtcNs
    completed_utc_ns: UtcNs
    host_class: str

    def __post_init__(self) -> None:
        Provenance(
            producer_name=self.producer_name,
            producer_version=self.producer_version,
            git_commit=self.git_commit,
            environment_digest=self.environment_digest,
            normalized_config_digest=Digest.sha256(b"model-context-validation"),
            input_digests=(Digest.sha256(b"model-context-input"),),
            dependency_digests=(),
            started_utc_ns=self.started_utc_ns,
            completed_utc_ns=self.completed_utc_ns,
            host_class=self.host_class,
        )


@dataclass(frozen=True)
class _Measurement:
    feature_set_id: str
    feature_id: str
    score: float
    variance: float | None


def receiver_quality_aggregate_algorithm_ref() -> ArtifactRef:
    descriptor = {
        "algorithm_id": ALGORITHM_ID,
        "algorithm_version": ALGORITHM_VERSION,
        "input": f"{FeatureDatasetSnapshot.SCHEMA_ID}/0.1",
        "feature_input": f"{FeatureSetBundle.SCHEMA_ID}/0.1",
        "output": f"{ModelSnapshotBundle.SCHEMA_ID}/0.1",
        "target": {
            "method_id": TARGET_METHOD_ID,
            "method_version": TARGET_METHOD_VERSION,
            "feature_kind": TARGET_FEATURE_KIND,
            "score_semantics": TARGET_SCORE_SEMANTICS,
            "score_units": PARAMETER_UNITS,
            "score_variance_units": f"{PARAMETER_UNITS}^2",
        },
        "estimator": {
            "complete_variance": "fixed-effect-inverse-variance-mean",
            "absent_variance": "iid-mean-with-between-recording-standard-error",
            "partial_variance": "not-identifiable",
            "sampling_unit": "one-feature-set-per-receiver",
        },
    }
    return ArtifactRef(
        f"{ALGORITHM_ID}-v0.1",
        canonical_digest(descriptor),
        SchemaRef("org.leo-flow.model-algorithm"),
    )


def receiver_quality_aggregate_config_ref(
    config: ReceiverQualityAggregateConfig,
) -> ArtifactRef:
    return ArtifactRef(
        f"{ALGORITHM_ID}-config-v0.1",
        canonical_digest(config),
        SchemaRef(CONFIG_SCHEMA_ID),
    )


class ReceiverQualityAggregateModel:
    """Fit a descriptive receiver-quality mean without raw-data capabilities."""

    def __init__(
        self,
        dataset_snapshot: FeatureDatasetSnapshot,
        config: ReceiverQualityAggregateConfig,
        execution: ModelExecutionContext,
    ) -> None:
        self._dataset = dataset_snapshot
        self._config = config
        self._execution = execution

    def fit(
        self,
        request: ModelAnalysisRequest,
        features: FeatureSetReader,
        ephemerides: EphemerisReader,
        hardware: HardwareMetadataReader,
    ) -> ModelSnapshotBundle:
        hardware_refs, ephemeris_refs = self._validate_request(request)
        hardware_by_receiver = self._load_hardware(hardware_refs, hardware)
        self._verify_ephemerides(ephemeris_refs, ephemerides)

        measurements: dict[str, list[_Measurement]] = {}
        warnings: set[str] = set()
        selected_count = 0
        for feature_ref in self._dataset.ordered_feature_set_refs:
            bundle = self._open_exact_feature_set(feature_ref, features)
            selected = [
                observation
                for observation in bundle.observations
                if self._is_target(observation)
            ]
            selected_count += len(selected)
            by_subject: dict[str, list[FeatureObservation]] = {}
            for observation in selected:
                if observation.receiver_chain_id is None:
                    warnings.add(
                        f"{bundle.feature_set_id}:{observation.feature_id}:"
                        "receiver-pair-not-supported"
                    )
                    continue
                by_subject.setdefault(str(observation.receiver_chain_id), []).append(
                    observation
                )
            for subject_id, observations in by_subject.items():
                if len(observations) != 1:
                    warnings.add(
                        f"{subject_id}:{bundle.feature_set_id}:"
                        f"ambiguous-observation-count:{len(observations)}"
                    )
                    continue
                observation = observations[0]
                hardware_matches = self._effective_hardware(
                    hardware_by_receiver.get(subject_id, ()),
                    observation.midpoint_utc_ns,
                )
                if len(hardware_matches) == 0:
                    warnings.add(
                        f"{subject_id}:{bundle.feature_set_id}:hardware-not-effective"
                    )
                    continue
                if len(hardware_matches) > 1:
                    warnings.add(
                        f"{subject_id}:{bundle.feature_set_id}:hardware-ambiguous"
                    )
                    continue
                variance = self._score_variance(
                    observation,
                    feature_set_id=str(bundle.feature_set_id),
                )
                measurements.setdefault(subject_id, []).append(
                    _Measurement(
                        feature_set_id=str(bundle.feature_set_id),
                        feature_id=str(observation.feature_id),
                        score=observation.score,
                        variance=variance,
                    )
                )

        parameters: list[ParameterEstimate] = []
        for subject_id in sorted(measurements):
            parameter, subject_warnings = self._estimate(
                subject_id, measurements[subject_id]
            )
            warnings.update(subject_warnings)
            if parameter is not None:
                parameters.append(parameter)
        if selected_count == 0:
            warnings.add("model:no-selected-quality-observations")
        if not parameters:
            warnings.add("model:no-identifiable-parameters")

        hardware_digests = tuple(ref.digest for ref in hardware_refs)
        ephemeris_digests = tuple(ref.normalized_digest for ref in ephemeris_refs)
        identity = self._scientific_identity(request, hardware_refs, ephemeris_refs)
        model_snapshot_id = ModelSnapshotId(
            f"model_{canonical_digest(identity).value[:32]}"
        )
        run_identity = {
            "model_identity": identity,
            "environment_digest": str(self._execution.environment_digest),
            "git_commit": self._execution.git_commit,
        }
        model_run_id = ModelRunId(f"mrun_{canonical_digest(run_identity).value[:32]}")
        feature_digests = tuple(
            ref.bundle_ref.digest for ref in self._dataset.ordered_feature_set_refs
        )
        dependency_digests = (
            (request.algorithm_ref.digest,)
            + hardware_digests
            + tuple(
                digest
                for ref in ephemeris_refs
                for digest in (ref.raw_digest, ref.normalized_digest)
            )
        )
        provenance = Provenance(
            producer_name=self._execution.producer_name,
            producer_version=self._execution.producer_version,
            git_commit=self._execution.git_commit,
            environment_digest=self._execution.environment_digest,
            normalized_config_digest=request.model_config_ref.digest,
            input_digests=(self._dataset.membership_digest,) + feature_digests,
            dependency_digests=dependency_digests,
            started_utc_ns=self._execution.started_utc_ns,
            completed_utc_ns=self._execution.completed_utc_ns,
            host_class=self._execution.host_class,
        )
        return ModelSnapshotBundle(
            schema=SchemaRef(ModelSnapshotBundle.SCHEMA_ID),
            model_snapshot_id=model_snapshot_id,
            model_run_id=model_run_id,
            dataset_membership_digest=self._dataset.membership_digest,
            hardware_snapshot_digests=hardware_digests,
            ephemeris_snapshot_digests=ephemeris_digests,
            provenance=provenance,
            parameters=tuple(parameters),
            warnings=tuple(sorted(warnings)),
        )

    def _validate_request(
        self, request: ModelAnalysisRequest
    ) -> tuple[
        tuple[HardwareMetadataSnapshotRef, ...], tuple[EphemerisSnapshotRef, ...]
    ]:
        expected_dataset_ref = FeatureDatasetSnapshotRef(
            self._dataset.snapshot_id, self._dataset.membership_digest
        )
        if request.dataset_snapshot_ref != expected_dataset_ref:
            raise ModelConfigurationError(
                "request dataset_snapshot_ref does not match injected snapshot"
            )
        if request.algorithm_ref != receiver_quality_aggregate_algorithm_ref():
            raise ModelConfigurationError(
                "request algorithm_ref does not identify this model"
            )
        if request.model_config_ref != receiver_quality_aggregate_config_ref(
            self._config
        ):
            raise ModelConfigurationError(
                "request model_config_ref does not match model config"
            )
        hardware_refs = tuple(
            sorted(
                request.hardware_metadata_snapshot_refs,
                key=lambda ref: (str(ref.snapshot_id), str(ref.digest)),
            )
        )
        if len({ref.snapshot_id for ref in hardware_refs}) != len(hardware_refs):
            raise ModelConfigurationError(
                "hardware_metadata_snapshot_refs contain duplicate snapshot IDs"
            )
        ephemeris_refs = tuple(
            sorted(
                request.ephemeris_snapshot_refs,
                key=lambda ref: (ref.source.value, str(ref.snapshot_id)),
            )
        )
        if len({ref.snapshot_id for ref in ephemeris_refs}) != len(ephemeris_refs):
            raise ModelConfigurationError(
                "ephemeris_snapshot_refs contain duplicate snapshot IDs"
            )
        return hardware_refs, ephemeris_refs

    @staticmethod
    def _load_hardware(
        refs: tuple[HardwareMetadataSnapshotRef, ...],
        reader: HardwareMetadataReader,
    ) -> dict[str, tuple[ReceiverChainMetadata, ...]]:
        by_receiver: dict[str, set[ReceiverChainMetadata]] = {}
        for ref in refs:
            snapshot = reader.get(ref)
            if snapshot.snapshot_id != ref.snapshot_id:
                raise ModelInputError(
                    f"hardware reader returned {snapshot.snapshot_id} for "
                    f"pinned {ref.snapshot_id}"
                )
            ids = [chain.receiver_chain_id for chain in snapshot.receiver_chains]
            if len(ids) != len(set(ids)):
                raise ModelInputError(
                    f"hardware snapshot {ref.snapshot_id} has duplicate receiver chains"
                )
            for chain in snapshot.receiver_chains:
                by_receiver.setdefault(str(chain.receiver_chain_id), set()).add(chain)
        return {
            subject: tuple(
                sorted(
                    chains,
                    key=lambda chain: (
                        int(chain.valid_from_utc_ns),
                        int(chain.valid_until_utc_ns or 2**63 - 1),
                        str(chain.radio_id),
                        chain.radio_channel,
                        chain.lnb_id,
                    ),
                )
            )
            for subject, chains in by_receiver.items()
        }

    @staticmethod
    def _verify_ephemerides(
        refs: tuple[EphemerisSnapshotRef, ...], reader: EphemerisReader
    ) -> None:
        for ref in refs:
            with reader.open(ref) as view:
                if view.ref != ref:
                    raise ModelInputError(
                        f"ephemeris reader returned {view.ref.snapshot_id} for "
                        f"pinned {ref.snapshot_id}"
                    )

    @staticmethod
    def _open_exact_feature_set(
        ref: FeatureSetRef, reader: FeatureSetReader
    ) -> FeatureSetBundle:
        with reader.open(ref) as view:
            actual_ref = view.ref
            if actual_ref != ref:
                raise ModelInputError(
                    f"feature reader did not return pinned membership {ref.feature_set_id}"
                )
            bundle = view.bundle()
        if bundle.feature_set_id != ref.feature_set_id:
            raise ModelInputError(
                f"feature bundle ID differs for pinned {ref.feature_set_id}"
            )
        if bundle.analysis_run_id != ref.analysis_run_id:
            raise ModelInputError(
                f"feature analysis run differs for pinned {ref.feature_set_id}"
            )
        return bundle

    @staticmethod
    def _is_target(observation: FeatureObservation) -> bool:
        return (
            observation.method_id == TARGET_METHOD_ID
            and observation.method_version == TARGET_METHOD_VERSION
            and observation.feature_kind == TARGET_FEATURE_KIND
            and observation.score_semantics == TARGET_SCORE_SEMANTICS
        )

    @staticmethod
    def _effective_hardware(
        candidates: tuple[ReceiverChainMetadata, ...], midpoint_utc_ns: UtcNs
    ) -> tuple[ReceiverChainMetadata, ...]:
        return tuple(
            candidate
            for candidate in candidates
            if candidate.valid_from_utc_ns <= midpoint_utc_ns
            and (
                candidate.valid_until_utc_ns is None
                or midpoint_utc_ns < candidate.valid_until_utc_ns
            )
        )

    def _score_variance(
        self, observation: FeatureObservation, *, feature_set_id: str
    ) -> float | None:
        values: list[Any] = [
            value
            for key, value in observation.uncertainty
            if key == self._config.score_variance_key
        ]
        if not values:
            return None
        if len(values) > 1:
            raise ModelInputError(
                f"{feature_set_id}:{observation.feature_id}:duplicate-score-variance"
            )
        value = values[0]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0.0
        ):
            raise ModelInputError(
                f"{feature_set_id}:{observation.feature_id}:"
                "score_variance-must-be-finite-positive"
            )
        return float(value)

    def _estimate(
        self, subject_id: str, values: list[_Measurement]
    ) -> tuple[ParameterEstimate | None, set[str]]:
        ordered = sorted(
            values,
            key=lambda item: (item.feature_set_id, item.feature_id),
        )
        warnings: set[str] = set()
        if len(ordered) < self._config.minimum_feature_sets:
            warnings.add(
                f"{subject_id}:insufficient-feature-sets:{len(ordered)}"
                f"<{self._config.minimum_feature_sets}"
            )
            return None, warnings
        variances = [item.variance for item in ordered]
        if any(value is None for value in variances) and any(
            value is not None for value in variances
        ):
            warnings.add(f"{subject_id}:partial-score-variance:not-identifiable")
            return None, warnings
        if all(value is not None for value in variances):
            weighted: list[tuple[float, float]] = []
            for item in ordered:
                if item.variance is None:  # narrowed by the branch above
                    raise AssertionError(
                        "complete variance branch contains no variance"
                    )
                weighted.append((item.score, item.variance))
            weighted.sort(key=lambda pair: (pair[0], pair[1]))
            variance_scale = min(variance for _, variance in weighted)
            scaled_weights = [variance_scale / variance for _, variance in weighted]
            total_scaled_weight = math.fsum(scaled_weights)
            mean = math.fsum(
                score * scaled_weight / total_scaled_weight
                for (score, _), scaled_weight in zip(
                    weighted, scaled_weights, strict=True
                )
            )
            covariance_value = max(
                variance_scale / total_scaled_weight,
                self._config.covariance_floor,
            )
            warnings.add(f"{subject_id}:covariance-mode:inverse-variance")
        else:
            scores = sorted(item.score for item in ordered)
            mean = math.fsum(score / len(scores) for score in scores)
            deviations = [score - mean for score in scores]
            deviation_scale = max(abs(value) for value in deviations)
            try:
                sample_variance = (
                    0.0
                    if deviation_scale == 0.0
                    else deviation_scale
                    * deviation_scale
                    * math.fsum((value / deviation_scale) ** 2 for value in deviations)
                    / (len(scores) - 1)
                )
            except OverflowError as exc:
                raise ModelInputError(
                    f"{subject_id}:aggregate-covariance-not-representable"
                ) from exc
            covariance_value = max(
                sample_variance / len(scores), self._config.covariance_floor
            )
            warnings.add(f"{subject_id}:covariance-mode:between-recording-scatter")
        if not math.isfinite(mean) or not math.isfinite(covariance_value):
            raise ModelInputError(f"{subject_id}:aggregate-result-not-finite")
        covariance = Covariance(
            basis=(PARAMETER_BASIS,),
            units=(PARAMETER_UNITS,),
            values=((covariance_value,),),
        )
        return (
            ParameterEstimate(
                parameter_id=PARAMETER_ID,
                subject_id=subject_id,
                value=(mean,),
                basis=(PARAMETER_BASIS,),
                units=(PARAMETER_UNITS,),
                covariance=covariance,
            ),
            warnings,
        )

    def _scientific_identity(
        self,
        request: ModelAnalysisRequest,
        hardware_refs: tuple[HardwareMetadataSnapshotRef, ...],
        ephemeris_refs: tuple[EphemerisSnapshotRef, ...],
    ) -> object:
        return {
            "dataset_snapshot_id": str(self._dataset.snapshot_id),
            "dataset_membership_digest": str(self._dataset.membership_digest),
            "algorithm_digest": str(request.algorithm_ref.digest),
            "config_digest": str(request.model_config_ref.digest),
            "hardware": [
                {
                    "snapshot_id": str(ref.snapshot_id),
                    "digest": str(ref.digest),
                }
                for ref in hardware_refs
            ],
            "ephemerides": [
                {
                    "snapshot_id": str(ref.snapshot_id),
                    "source": ref.source.value,
                    "raw_digest": str(ref.raw_digest),
                    "normalized_digest": str(ref.normalized_digest),
                }
                for ref in ephemeris_refs
            ],
        }
