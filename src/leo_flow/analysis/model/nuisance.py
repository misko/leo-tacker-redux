"""Replayable robust batch fit of relative radio and LNB nuisance terms."""

from __future__ import annotations

import math
from dataclasses import dataclass

from leo_flow.contracts.core import (
    ArtifactRef,
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

from .api import ModelConfigurationError, ModelExecutionContext, ModelInputError
from .simulator import FEATURE_KIND, METHOD_ID, METHOD_VERSION

ALGORITHM_ID = "relative-radio-lnb-nuisance"
ALGORITHM_VERSION = "0.1.0"
CONFIG_SCHEMA_ID = "org.leo-flow.relative-radio-lnb-nuisance-config"


@dataclass(frozen=True)
class NuisanceBatchConfig:
    minimum_measurements: int = 3
    frequency_variance_hz2: float = 25.0
    drift_variance_hz2_s2: float = 0.01
    huber_threshold_sigma: float = 2.5
    maximum_robust_iterations: int = 12
    convergence_tolerance: float = 1e-10
    covariance_floor: float = 1e-15

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_measurements, bool)
            or not isinstance(self.minimum_measurements, int)
            or self.minimum_measurements < 2
        ):
            raise ValueError("minimum_measurements must be an integer >= 2")
        if (
            isinstance(self.maximum_robust_iterations, bool)
            or not isinstance(self.maximum_robust_iterations, int)
            or self.maximum_robust_iterations < 1
        ):
            raise ValueError("maximum_robust_iterations must be positive")
        for name in (
            "frequency_variance_hz2",
            "drift_variance_hz2_s2",
            "huber_threshold_sigma",
            "convergence_tolerance",
            "covariance_floor",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0.0
            ):
                raise ValueError(f"{name} must be finite and positive")


def nuisance_batch_algorithm_ref() -> ArtifactRef:
    return ArtifactRef(
        f"{ALGORITHM_ID}-v0.1",
        canonical_digest(
            {
                "algorithm_id": ALGORITHM_ID,
                "algorithm_version": ALGORITHM_VERSION,
                "input_feature": {
                    "method_id": METHOD_ID,
                    "method_version": METHOD_VERSION,
                    "feature_kind": FEATURE_KIND,
                    "values": ["frequency_offset_hz", "drift_hz_s"],
                },
                "equation": "residual=radio+lnb+error",
                "gauge": "lexicographically-first-radio-per-connected-component=0",
                "estimator": "independent-dimension-huber-wls",
            }
        ),
        SchemaRef("org.leo-flow.model-algorithm"),
    )


def nuisance_batch_config_ref(config: NuisanceBatchConfig) -> ArtifactRef:
    return ArtifactRef(
        f"{ALGORITHM_ID}-config-v0.1",
        canonical_digest(config),
        SchemaRef(CONFIG_SCHEMA_ID),
    )


@dataclass(frozen=True)
class _Measurement:
    feature_set_id: str
    feature_id: str
    radio_id: str
    lnb_id: str
    value: float
    variance: float


@dataclass(frozen=True)
class _Fit:
    values: dict[tuple[str, str], float]
    variances: dict[tuple[str, str], float]
    reference_radios: tuple[str, ...]
    downweighted: int


class RelativeRadioLnbNuisanceModel:
    """Fit relative additive terms without raw IQ or mutable catalog access.

    Absolute radio and LNB contributions have a gauge freedom.  Each connected
    radio/LNB component therefore pins its lexicographically first radio to
    zero and reports that convention in warnings and parameter covariance.
    """

    def __init__(
        self,
        dataset_snapshot: FeatureDatasetSnapshot,
        config: NuisanceBatchConfig,
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
        frequency: list[_Measurement] = []
        drift: list[_Measurement] = []
        warnings: set[str] = set()
        for feature_ref in self._dataset.ordered_feature_set_refs:
            bundle = self._open_exact_feature_set(feature_ref, features)
            for observation in bundle.observations:
                if not self._is_target(observation):
                    continue
                if observation.receiver_chain_id is None:
                    warnings.add(
                        f"{observation.feature_id}:receiver-pair-not-supported"
                    )
                    continue
                candidates = self._effective_hardware(
                    hardware_by_receiver.get(str(observation.receiver_chain_id), ()),
                    int(observation.midpoint_utc_ns),
                )
                if len(candidates) != 1:
                    state = "not-effective" if not candidates else "ambiguous"
                    warnings.add(f"{observation.feature_id}:hardware-{state}")
                    continue
                chain = candidates[0]
                values = self._measurement_values(observation, warnings)
                if values[0] is not None:
                    frequency.append(
                        _Measurement(
                            str(bundle.feature_set_id),
                            str(observation.feature_id),
                            str(chain.radio_id),
                            chain.lnb_id,
                            values[0][0],
                            values[0][1],
                        )
                    )
                if values[1] is not None:
                    drift.append(
                        _Measurement(
                            str(bundle.feature_set_id),
                            str(observation.feature_id),
                            str(chain.radio_id),
                            chain.lnb_id,
                            values[1][0],
                            values[1][1],
                        )
                    )

        parameters: list[ParameterEstimate] = []
        for measurements, quantity, parameter_suffix, basis_name, unit in (
            (
                frequency,
                "frequency",
                "frequency-offset",
                "frequency_offset_hz",
                "Hz",
            ),
            (drift, "drift", "frequency-drift", "drift_hz_s", "Hz/s"),
        ):
            fit = self._fit_dimension(measurements, quantity, warnings)
            if fit is None:
                continue
            for radio_id in fit.reference_radios:
                warnings.add(f"{quantity}:gauge-reference-radio:{radio_id}")
            if fit.downweighted:
                warnings.add(f"{quantity}:robust-downweighted:{fit.downweighted}")
            basis = (basis_name,)
            for (kind, subject_id), value in sorted(fit.values.items()):
                parameters.append(
                    ParameterEstimate(
                        parameter_id=f"{kind}-{parameter_suffix}",
                        subject_id=subject_id,
                        value=(value,),
                        basis=basis,
                        units=(unit,),
                        covariance=self._covariance(
                            basis, unit, fit.variances[(kind, subject_id)]
                        ),
                    )
                )
        if not parameters:
            warnings.add("model:no-identifiable-parameters")
        return self._bundle(
            request,
            hardware_refs,
            ephemeris_refs,
            tuple(
                sorted(
                    parameters, key=lambda item: (item.parameter_id, item.subject_id)
                )
            ),
            tuple(sorted(warnings)),
        )

    @staticmethod
    def _covariance(basis: tuple[str], unit: str, value: float) -> Covariance:
        return Covariance(basis, (unit,), ((value,),))

    def _fit_dimension(
        self,
        measurements: list[_Measurement],
        quantity: str,
        warnings: set[str],
    ) -> _Fit | None:
        ordered = sorted(
            measurements,
            key=lambda item: (
                item.radio_id,
                item.lnb_id,
                item.feature_set_id,
                item.feature_id,
            ),
        )
        if len(ordered) < self._config.minimum_measurements:
            warnings.add(
                f"{quantity}:insufficient-measurements:{len(ordered)}"
                f"<{self._config.minimum_measurements}"
            )
            return None
        components = _components(ordered)
        references = tuple(sorted(min(radios) for radios, _ in components))
        subjects = sorted(
            {
                *(
                    ("radio", item.radio_id)
                    for item in ordered
                    if item.radio_id not in references
                ),
                *(("lnb", item.lnb_id) for item in ordered),
            }
        )
        if not subjects:
            warnings.add(f"{quantity}:rank-deficient:no-free-parameters")
            return None
        index = {subject: position for position, subject in enumerate(subjects)}
        rows: list[list[float]] = []
        for item in ordered:
            row = [0.0] * len(subjects)
            if item.radio_id not in references:
                row[index[("radio", item.radio_id)]] = 1.0
            row[index[("lnb", item.lnb_id)]] = 1.0
            rows.append(row)
        base_weights = [1.0 / item.variance for item in ordered]
        robust = [1.0] * len(ordered)
        solution: list[float] | None = None
        inverse: list[list[float]] | None = None
        for _ in range(self._config.maximum_robust_iterations):
            weights = [
                base * multiplier
                for base, multiplier in zip(base_weights, robust, strict=True)
            ]
            solved = _weighted_least_squares(rows, ordered, weights)
            if solved is None:
                warnings.add(f"{quantity}:rank-deficient")
                return None
            candidate, candidate_inverse = solved
            updated: list[float] = []
            for row, item in zip(rows, ordered, strict=True):
                residual_sigma = abs(
                    item.value
                    - math.fsum(a * b for a, b in zip(row, candidate, strict=True))
                ) / math.sqrt(item.variance)
                updated.append(
                    1.0
                    if residual_sigma <= self._config.huber_threshold_sigma
                    else self._config.huber_threshold_sigma / residual_sigma
                )
            delta = max(abs(a - b) for a, b in zip(updated, robust, strict=True))
            solution, inverse, robust = candidate, candidate_inverse, updated
            if delta <= self._config.convergence_tolerance:
                break
        final = _weighted_least_squares(
            rows,
            ordered,
            [
                base * multiplier
                for base, multiplier in zip(base_weights, robust, strict=True)
            ],
        )
        if final is None:
            warnings.add(f"{quantity}:rank-deficient")
            return None
        solution, inverse = final
        values = {subject: solution[position] for subject, position in index.items()}
        variances = {
            subject: max(inverse[position][position], self._config.covariance_floor)
            for subject, position in index.items()
        }
        for radio_id in references:
            values[("radio", radio_id)] = 0.0
            variances[("radio", radio_id)] = self._config.covariance_floor
        return _Fit(
            values,
            variances,
            references,
            sum(value < 1.0 - self._config.convergence_tolerance for value in robust),
        )

    def _measurement_values(
        self, observation: FeatureObservation, warnings: set[str]
    ) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
        variances = {
            "frequency_offset_hz": self._config.frequency_variance_hz2,
            "drift_hz_s": self._config.drift_variance_hz2_s2,
        }
        if observation.covariance is not None:
            covariance = observation.covariance
            expected_units = {
                "frequency_offset_hz": "Hz",
                "drift_hz_s": "Hz/s",
            }
            for field, expected_unit in expected_units.items():
                if field in covariance.basis:
                    position = covariance.basis.index(field)
                    if covariance.units[position] != expected_unit:
                        raise ModelInputError(
                            f"{observation.feature_id}:{field}:unit-must-be-{expected_unit}"
                        )
                    variance = covariance.values[position][position]
                    if variance <= 0.0:
                        raise ModelInputError(
                            f"{observation.feature_id}:{field}:variance-not-positive"
                        )
                    variances[field] = variance
            if any(
                covariance.values[i][j] != 0.0
                for i in range(len(covariance.basis))
                for j in range(len(covariance.basis))
                if i != j
            ):
                warnings.add("model:input-cross-covariance-ignored")
        frequency = (
            None
            if observation.frequency_offset_hz is None
            else (observation.frequency_offset_hz, variances["frequency_offset_hz"])
        )
        drift = (
            None
            if observation.drift_hz_s is None
            else (observation.drift_hz_s, variances["drift_hz_s"])
        )
        return frequency, drift

    def _validate_request(
        self, request: ModelAnalysisRequest
    ) -> tuple[
        tuple[HardwareMetadataSnapshotRef, ...], tuple[EphemerisSnapshotRef, ...]
    ]:
        expected_dataset = FeatureDatasetSnapshotRef(
            self._dataset.snapshot_id, self._dataset.membership_digest
        )
        if request.dataset_snapshot_ref != expected_dataset:
            raise ModelConfigurationError("request dataset_snapshot_ref differs")
        if request.algorithm_ref != nuisance_batch_algorithm_ref():
            raise ModelConfigurationError("request algorithm_ref differs")
        if request.model_config_ref != nuisance_batch_config_ref(self._config):
            raise ModelConfigurationError("request model_config_ref differs")
        hardware_refs = tuple(
            sorted(
                request.hardware_metadata_snapshot_refs,
                key=lambda ref: (str(ref.snapshot_id), str(ref.digest)),
            )
        )
        if len({ref.snapshot_id for ref in hardware_refs}) != len(hardware_refs):
            raise ModelConfigurationError("duplicate hardware snapshot IDs")
        ephemeris_refs = tuple(
            sorted(
                request.ephemeris_snapshot_refs,
                key=lambda ref: (ref.source.value, str(ref.snapshot_id)),
            )
        )
        if len({ref.snapshot_id for ref in ephemeris_refs}) != len(ephemeris_refs):
            raise ModelConfigurationError("duplicate ephemeris snapshot IDs")
        return hardware_refs, ephemeris_refs

    @staticmethod
    def _load_hardware(
        refs: tuple[HardwareMetadataSnapshotRef, ...], reader: HardwareMetadataReader
    ) -> dict[str, tuple[ReceiverChainMetadata, ...]]:
        by_receiver: dict[str, set[ReceiverChainMetadata]] = {}
        for ref in refs:
            snapshot = reader.get(ref)
            if snapshot.snapshot_id != ref.snapshot_id:
                raise ModelInputError("hardware reader substituted snapshot")
            for chain in snapshot.receiver_chains:
                by_receiver.setdefault(str(chain.receiver_chain_id), set()).add(chain)
        return {
            receiver: tuple(
                sorted(
                    chains,
                    key=lambda chain: (
                        int(chain.valid_from_utc_ns),
                        int(chain.valid_until_utc_ns or 2**63 - 1),
                        str(chain.radio_id),
                        chain.lnb_id,
                    ),
                )
            )
            for receiver, chains in by_receiver.items()
        }

    @staticmethod
    def _verify_ephemerides(
        refs: tuple[EphemerisSnapshotRef, ...], reader: EphemerisReader
    ) -> None:
        for ref in refs:
            with reader.open(ref) as view:
                if view.ref != ref:
                    raise ModelInputError("ephemeris reader substituted snapshot")

    @staticmethod
    def _open_exact_feature_set(
        ref: FeatureSetRef, reader: FeatureSetReader
    ) -> FeatureSetBundle:
        with reader.open(ref) as view:
            if view.ref != ref:
                raise ModelInputError("feature reader did not return pinned membership")
            bundle = view.bundle()
        if (
            bundle.feature_set_id != ref.feature_set_id
            or bundle.analysis_run_id != ref.analysis_run_id
        ):
            raise ModelInputError("feature bundle differs from pinned membership")
        return bundle

    @staticmethod
    def _effective_hardware(
        candidates: tuple[ReceiverChainMetadata, ...], midpoint_utc_ns: int
    ) -> tuple[ReceiverChainMetadata, ...]:
        return tuple(
            candidate
            for candidate in candidates
            if int(candidate.valid_from_utc_ns) <= midpoint_utc_ns
            and (
                candidate.valid_until_utc_ns is None
                or midpoint_utc_ns < int(candidate.valid_until_utc_ns)
            )
        )

    @staticmethod
    def _is_target(observation: FeatureObservation) -> bool:
        return (
            observation.method_id == METHOD_ID
            and observation.method_version == METHOD_VERSION
            and observation.feature_kind == FEATURE_KIND
        )

    def _bundle(
        self,
        request: ModelAnalysisRequest,
        hardware_refs: tuple[HardwareMetadataSnapshotRef, ...],
        ephemeris_refs: tuple[EphemerisSnapshotRef, ...],
        parameters: tuple[ParameterEstimate, ...],
        warnings: tuple[str, ...],
    ) -> ModelSnapshotBundle:
        hardware_digests = tuple(ref.digest for ref in hardware_refs)
        ephemeris_digests = tuple(ref.normalized_digest for ref in ephemeris_refs)
        identity = {
            "dataset": str(self._dataset.membership_digest),
            "algorithm": str(request.algorithm_ref.digest),
            "config": str(request.model_config_ref.digest),
            "hardware": [str(value) for value in hardware_digests],
            "ephemerides": [str(value) for value in ephemeris_digests],
        }
        token = canonical_digest(identity).value
        feature_digests = tuple(
            ref.bundle_ref.digest for ref in self._dataset.ordered_feature_set_refs
        )
        provenance = Provenance(
            self._execution.producer_name,
            self._execution.producer_version,
            self._execution.git_commit,
            self._execution.environment_digest,
            request.model_config_ref.digest,
            (self._dataset.membership_digest,) + feature_digests,
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
        run_token = canonical_digest(
            {
                "scientific_identity": identity,
                "environment": str(self._execution.environment_digest),
                "git_commit": self._execution.git_commit,
            }
        ).value
        return ModelSnapshotBundle(
            SchemaRef(ModelSnapshotBundle.SCHEMA_ID),
            ModelSnapshotId(f"model_{token[:32]}"),
            ModelRunId(f"mrun_{run_token[:32]}"),
            self._dataset.membership_digest,
            hardware_digests,
            ephemeris_digests,
            provenance,
            parameters,
            warnings,
        )


def _components(
    measurements: list[_Measurement],
) -> tuple[tuple[set[str], set[str]], ...]:
    remaining_radios = {item.radio_id for item in measurements}
    edges = {(item.radio_id, item.lnb_id) for item in measurements}
    components: list[tuple[set[str], set[str]]] = []
    while remaining_radios:
        radios = {min(remaining_radios)}
        lnbs: set[str] = set()
        changed = True
        while changed:
            changed = False
            for radio_id, lnb_id in edges:
                if radio_id in radios and lnb_id not in lnbs:
                    lnbs.add(lnb_id)
                    changed = True
                if lnb_id in lnbs and radio_id not in radios:
                    radios.add(radio_id)
                    changed = True
        remaining_radios -= radios
        components.append((radios, lnbs))
    return tuple(components)


def _weighted_least_squares(
    rows: list[list[float]],
    measurements: list[_Measurement],
    weights: list[float],
) -> tuple[list[float], list[list[float]]] | None:
    size = len(rows[0])
    normal = [[0.0] * size for _ in range(size)]
    target = [0.0] * size
    for row, measurement, weight in zip(rows, measurements, weights, strict=True):
        for i in range(size):
            target[i] += weight * row[i] * measurement.value
            for j in range(size):
                normal[i][j] += weight * row[i] * row[j]
    inverse = _inverse(normal)
    if inverse is None:
        return None
    solution = [
        math.fsum(inverse[i][j] * target[j] for j in range(size)) for i in range(size)
    ]
    if not all(math.isfinite(value) for value in solution):
        return None
    return solution, inverse


def _inverse(matrix: list[list[float]]) -> list[list[float]] | None:
    size = len(matrix)
    scale = max((abs(value) for row in matrix for value in row), default=0.0)
    if scale == 0.0 or not math.isfinite(scale):
        return None
    augmented = [
        [value / scale for value in row] + [1.0 if i == j else 0.0 for j in range(size)]
        for i, row in enumerate(matrix)
    ]
    tolerance = 1e-12
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= tolerance:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        pivot_value = augmented[column][column]
        augmented[column] = [value / pivot_value for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(
                    augmented[row], augmented[column], strict=True
                )
            ]
    return [[augmented[i][size + j] / scale for j in range(size)] for i in range(size)]
