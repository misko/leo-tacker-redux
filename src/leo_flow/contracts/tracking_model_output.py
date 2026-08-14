"""Production output contract for joint RF nuisance estimation, not orbit state."""

from __future__ import annotations

from dataclasses import dataclass

from ._validation import require_finite, require_token, require_utc_ns
from .core import (
    V0_1,
    ArtifactRef,
    Digest,
    EphemerisSnapshotId,
    ModelRunId,
    ModelSnapshotId,
    Provenance,
    ReceiverChainId,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from .ephemeris import EphemerisSource, RecordingInterval
from .features import Covariance
from .hardware import HardwareMetadataSnapshotRef
from .tracking_input import (
    RF_CALIBRATION_BASIS,
    RF_UNITS,
    DurableDatasetIdentity,
    TrackingInputSnapshotIdentity,
)

SATELLITE_CARRIER_RESIDUAL_BASIS = (
    "carrier_frequency_residual_hz",
    "carrier_frequency_drift_residual_hz_s",
)
JOINT_COVARIANCE_PSD_TOLERANCE = 1e-10
MAX_PARAMETER_BLOCKS = 512
MAX_ASSOCIATION_OUTCOMES = 100_000
MAX_WARNINGS = 1_024
NOT_ORBIT_STATE_WARNING = "not-orbit-state-estimate"


@dataclass(frozen=True)
class TrackingHardwareEvidence:
    snapshot_ref: HardwareMetadataSnapshotRef
    link_digests: tuple[Digest, ...]

    def __post_init__(self) -> None:
        _canonical_digests(self.link_digests, "hardware link digests")


@dataclass(frozen=True)
class TrackingCalibrationEvidence:
    calibration_ref: ArtifactRef
    source_digests: tuple[Digest, ...]

    def __post_init__(self) -> None:
        _schema_artifact(self.calibration_ref, "calibration_ref")
        _canonical_digests(self.source_digests, "calibration source digests")


@dataclass(frozen=True)
class TrackingEphemerisEvidence:
    source: EphemerisSource
    snapshot_id: EphemerisSnapshotId
    raw_digest: Digest
    normalized_digest: Digest
    link_digests: tuple[Digest, ...]
    selection_policy_digests: tuple[Digest, ...]

    def __post_init__(self) -> None:
        _canonical_digests(self.link_digests, "ephemeris link digests")
        _canonical_digests(
            self.selection_policy_digests, "ephemeris selection policy digests"
        )


@dataclass(frozen=True)
class TrackingModelEvidence:
    """Canonical scientific closure for one verified tracking-input snapshot."""

    tracking_input_identity: TrackingInputSnapshotIdentity
    durable_dataset: DurableDatasetIdentity
    ordered_entry_count: int
    ordered_entry_digest: Digest
    hardware: tuple[TrackingHardwareEvidence, ...]
    calibrations: tuple[TrackingCalibrationEvidence, ...]
    ephemerides: tuple[TrackingEphemerisEvidence, ...]
    carrier_hypothesis_refs: tuple[ArtifactRef, ...]
    prediction_policy_refs: tuple[ArtifactRef, ...]
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    propagator_ref: ArtifactRef
    gravity_model_ref: ArtifactRef
    time_scale_ref: ArtifactRef
    earth_orientation_ref: ArtifactRef
    error_policy_ref: ArtifactRef

    def __post_init__(self) -> None:
        if (
            isinstance(self.ordered_entry_count, bool)
            or not isinstance(self.ordered_entry_count, int)
            or not 0 < self.ordered_entry_count <= MAX_ASSOCIATION_OUTCOMES
        ):
            raise ValueError("ordered_entry_count is outside the supported bound")
        if self.ordered_entry_digest != self.tracking_input_identity.membership_digest:
            raise ValueError("ordered entry digest differs from tracking input")
        if not self.hardware or self.hardware != tuple(
            sorted(self.hardware, key=_hardware_key)
        ):
            raise ValueError("hardware evidence must be non-empty and canonical")
        if len({item.snapshot_ref.snapshot_id for item in self.hardware}) != len(
            self.hardware
        ):
            raise ValueError("hardware evidence snapshot IDs are duplicated")
        if len({item.snapshot_ref.digest for item in self.hardware}) != len(
            self.hardware
        ):
            raise ValueError("hardware evidence snapshot digests are duplicated")
        _unique_grouped_digests(
            tuple(item.link_digests for item in self.hardware),
            "hardware evidence link digests",
        )
        if not self.calibrations or self.calibrations != tuple(
            sorted(self.calibrations, key=_calibration_key)
        ):
            raise ValueError("calibration evidence must be non-empty and canonical")
        if len({item.calibration_ref.artifact_id for item in self.calibrations}) != len(
            self.calibrations
        ):
            raise ValueError("calibration evidence IDs are duplicated")
        if not self.ephemerides or self.ephemerides != tuple(
            sorted(self.ephemerides, key=_ephemeris_key)
        ):
            raise ValueError("ephemeris evidence must be non-empty and canonical")
        if len({item.snapshot_id for item in self.ephemerides}) != len(
            self.ephemerides
        ):
            raise ValueError("ephemeris evidence snapshot IDs are duplicated")
        if len({item.raw_digest for item in self.ephemerides}) != len(
            self.ephemerides
        ) or len({item.normalized_digest for item in self.ephemerides}) != len(
            self.ephemerides
        ):
            raise ValueError("ephemeris evidence snapshot digests are duplicated")
        _unique_grouped_digests(
            tuple(item.link_digests for item in self.ephemerides),
            "ephemeris evidence link digests",
        )
        _canonical_artifacts(
            self.carrier_hypothesis_refs, "carrier hypothesis references"
        )
        _canonical_artifacts(
            self.prediction_policy_refs, "prediction policy references"
        )
        for name in (
            "algorithm_ref",
            "config_ref",
            "propagator_ref",
            "gravity_model_ref",
            "time_scale_ref",
            "earth_orientation_ref",
            "error_policy_ref",
        ):
            _schema_artifact(getattr(self, name), name)

    def evidence_digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class ReceiverLnbFrequencyEstimate:
    """Jointly estimated receiver/LNB bias and drift over one validity interval."""

    receiver_chain_id: ReceiverChainId
    hardware_snapshot_ref: HardwareMetadataSnapshotRef
    validity: RecordingInterval
    observation_count: int
    value: tuple[float, float]
    basis: tuple[str, str]
    units: tuple[str, str]
    covariance: Covariance

    def __post_init__(self) -> None:
        _estimate(
            self.value,
            self.basis,
            self.units,
            self.covariance,
            RF_CALIBRATION_BASIS,
            self.observation_count,
            "receiver/LNB estimate",
        )

    def joint_basis(self) -> tuple[str, str]:
        prefix = (
            f"receiver:{self.receiver_chain_id}:{int(self.validity.started_utc_ns)}"
        )
        return f"{prefix}:{self.basis[0]}", f"{prefix}:{self.basis[1]}"


@dataclass(frozen=True)
class SatelliteCarrierResidualEstimate:
    """Transmitter/carrier residual only; this is explicitly not an orbit state."""

    norad_id: int
    carrier_hypothesis_ref: ArtifactRef
    validity: RecordingInterval
    observation_count: int
    value: tuple[float, float]
    basis: tuple[str, str]
    units: tuple[str, str]
    covariance: Covariance

    def __post_init__(self) -> None:
        if isinstance(self.norad_id, bool) or not isinstance(self.norad_id, int):
            raise TypeError("norad_id must be an integer")
        if self.norad_id <= 0:
            raise ValueError("norad_id must be positive")
        _schema_artifact(self.carrier_hypothesis_ref, "carrier_hypothesis_ref")
        _estimate(
            self.value,
            self.basis,
            self.units,
            self.covariance,
            SATELLITE_CARRIER_RESIDUAL_BASIS,
            self.observation_count,
            "satellite carrier residual estimate",
        )

    def joint_basis(self) -> tuple[str, str]:
        carrier = self.carrier_hypothesis_ref
        prefix = (
            f"satellite:{self.norad_id}:carrier:{carrier.artifact_id}:"
            f"{carrier.digest}:{int(self.validity.started_utc_ns)}"
        )
        return f"{prefix}:{self.basis[0]}", f"{prefix}:{self.basis[1]}"


@dataclass(frozen=True)
class CarrierAssociationCandidate:
    norad_id: int
    carrier_hypothesis_ref: ArtifactRef

    def __post_init__(self) -> None:
        _norad_id(self.norad_id)
        _schema_artifact(self.carrier_hypothesis_ref, "carrier_hypothesis_ref")


@dataclass(frozen=True)
class AcceptedAssociationEvidence:
    entry_index: int
    observed_utc_ns: UtcNs
    receiver_chain_id: ReceiverChainId
    selected_candidate: CarrierAssociationCandidate
    candidates: tuple[CarrierAssociationCandidate, ...]
    decision_digest: Digest

    def __post_init__(self) -> None:
        _entry_index(self.entry_index)
        require_utc_ns(self.observed_utc_ns, "observed_utc_ns")
        _carrier_candidates(self.candidates)
        if self.selected_candidate not in self.candidates:
            raise ValueError("selected carrier hypothesis is absent from candidates")


@dataclass(frozen=True)
class RejectedAssociationEvidence:
    entry_index: int
    observed_utc_ns: UtcNs
    reason_code: str
    candidates: tuple[CarrierAssociationCandidate, ...]
    decision_digest: Digest

    def __post_init__(self) -> None:
        _entry_index(self.entry_index)
        require_utc_ns(self.observed_utc_ns, "observed_utc_ns")
        require_token(self.reason_code, "reason_code")
        if self.candidates:
            _carrier_candidates(self.candidates)


ParameterBlock = ReceiverLnbFrequencyEstimate | SatelliteCarrierResidualEstimate


@dataclass(frozen=True)
class TrackingModelSnapshotBundle:
    """Joint RF nuisance result; no field represents physical orbit correction."""

    schema: SchemaRef
    model_snapshot_id: ModelSnapshotId
    model_run_id: ModelRunId
    evidence: TrackingModelEvidence
    provenance: Provenance
    receiver_lnb_estimates: tuple[ReceiverLnbFrequencyEstimate, ...]
    satellite_carrier_residual_estimates: tuple[SatelliteCarrierResidualEstimate, ...]
    joint_covariance: Covariance
    accepted_associations: tuple[AcceptedAssociationEvidence, ...]
    rejected_associations: tuple[RejectedAssociationEvidence, ...]
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.tracking-model-snapshot-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported tracking model snapshot schema")
        blocks = self.receiver_lnb_estimates + self.satellite_carrier_residual_estimates
        if not blocks or len(blocks) > MAX_PARAMETER_BLOCKS:
            raise ValueError("tracking model parameter block count is invalid")
        if self.receiver_lnb_estimates != tuple(
            sorted(self.receiver_lnb_estimates, key=_receiver_key)
        ) or self.satellite_carrier_residual_estimates != tuple(
            sorted(self.satellite_carrier_residual_estimates, key=_satellite_key)
        ):
            raise ValueError("tracking model estimates are not canonical")
        if len({_receiver_key(item) for item in self.receiver_lnb_estimates}) != len(
            self.receiver_lnb_estimates
        ) or len(
            {_satellite_key(item) for item in self.satellite_carrier_residual_estimates}
        ) != len(self.satellite_carrier_residual_estimates):
            raise ValueError("tracking model estimate identities are duplicated")
        carrier_hypotheses = set(self.evidence.carrier_hypothesis_refs)
        referenced_hypotheses = {
            item.carrier_hypothesis_ref
            for item in self.satellite_carrier_residual_estimates
        }
        referenced_hypotheses.update(
            candidate.carrier_hypothesis_ref
            for outcome in self.accepted_associations + self.rejected_associations
            for candidate in outcome.candidates
        )
        if referenced_hypotheses != carrier_hypotheses:
            raise ValueError("carrier hypothesis evidence is not exactly closed")
        hardware_refs = {item.snapshot_ref for item in self.evidence.hardware}
        if any(
            item.hardware_snapshot_ref not in hardware_refs
            for item in self.receiver_lnb_estimates
        ):
            raise ValueError("receiver estimate hardware evidence is not closed")
        self._validate_joint_covariance(blocks)
        self._validate_associations()
        if (
            not self.warnings
            or len(self.warnings) > MAX_WARNINGS
            or self.warnings != tuple(sorted(set(self.warnings)))
            or NOT_ORBIT_STATE_WARNING not in self.warnings
        ):
            raise ValueError("tracking model warnings are not canonical or complete")
        if self.provenance.normalized_config_digest != self.evidence.config_ref.digest:
            raise ValueError("tracking model configuration provenance differs")
        if self.provenance.input_digests != tracking_model_input_digests(self.evidence):
            raise ValueError("tracking model input provenance is not closed")
        if self.provenance.dependency_digests != tracking_model_dependency_digests(
            self.evidence
        ):
            raise ValueError("tracking model dependency provenance is not closed")
        snapshot_digest = tracking_model_snapshot_digest(
            self.schema,
            self.evidence,
            self.receiver_lnb_estimates,
            self.satellite_carrier_residual_estimates,
            self.joint_covariance,
            self.accepted_associations,
            self.rejected_associations,
            self.warnings,
        )
        if self.model_snapshot_id != ModelSnapshotId(
            f"model_{snapshot_digest.value[:32]}"
        ):
            raise ValueError("tracking model snapshot ID differs from content")
        run_digest = canonical_digest(
            {"snapshot_digest": snapshot_digest, "provenance": self.provenance}
        )
        if self.model_run_id != ModelRunId(f"mrun_{run_digest.value[:32]}"):
            raise ValueError("tracking model run ID differs from content")

    def _validate_joint_covariance(self, blocks: tuple[ParameterBlock, ...]) -> None:
        expected_basis = tuple(name for block in blocks for name in block.joint_basis())
        expected_units = tuple(unit for _block in blocks for unit in RF_UNITS)
        covariance = self.joint_covariance
        if (
            covariance.basis != expected_basis
            or covariance.units != expected_units
            or covariance.psd_tolerance != JOINT_COVARIANCE_PSD_TOLERANCE
        ):
            raise ValueError("joint covariance basis, units, or tolerance differs")
        _exact_symmetric(covariance, "joint covariance")
        for index, block in enumerate(blocks):
            start = index * 2
            expected = block.covariance.values
            actual = tuple(
                tuple(
                    covariance.values[start + row][start + column]
                    for column in range(2)
                )
                for row in range(2)
            )
            if actual != expected:
                raise ValueError("marginal covariance differs from joint block")

    def _validate_associations(self) -> None:
        if self.accepted_associations != tuple(
            sorted(self.accepted_associations, key=lambda item: item.entry_index)
        ) or self.rejected_associations != tuple(
            sorted(self.rejected_associations, key=lambda item: item.entry_index)
        ):
            raise ValueError("association evidence is not canonical")
        outcomes = self.accepted_associations + self.rejected_associations
        indices = sorted(item.entry_index for item in outcomes)
        if indices != list(range(self.evidence.ordered_entry_count)):
            raise ValueError("association evidence does not cover ordered entries")
        for association in self.accepted_associations:
            receiver_matches = tuple(
                item
                for item in self.receiver_lnb_estimates
                if item.receiver_chain_id == association.receiver_chain_id
                and _contains(item.validity, association.observed_utc_ns)
            )
            satellite_matches = tuple(
                item
                for item in self.satellite_carrier_residual_estimates
                if item.norad_id == association.selected_candidate.norad_id
                and item.carrier_hypothesis_ref
                == association.selected_candidate.carrier_hypothesis_ref
                and _contains(item.validity, association.observed_utc_ns)
            )
            if len(receiver_matches) != 1 or len(satellite_matches) != 1:
                raise ValueError("accepted association has no unique parameter support")
        for receiver_estimate in self.receiver_lnb_estimates:
            count = sum(
                item.receiver_chain_id == receiver_estimate.receiver_chain_id
                and _contains(receiver_estimate.validity, item.observed_utc_ns)
                for item in self.accepted_associations
            )
            if count != receiver_estimate.observation_count:
                raise ValueError("receiver/LNB observation support count differs")
        for satellite_estimate in self.satellite_carrier_residual_estimates:
            count = sum(
                item.selected_candidate.norad_id == satellite_estimate.norad_id
                and item.selected_candidate.carrier_hypothesis_ref
                == satellite_estimate.carrier_hypothesis_ref
                and _contains(satellite_estimate.validity, item.observed_utc_ns)
                for item in self.accepted_associations
            )
            if count != satellite_estimate.observation_count:
                raise ValueError("satellite observation support count differs")


def tracking_model_input_digests(evidence: TrackingModelEvidence) -> tuple[Digest, ...]:
    values = [
        evidence.tracking_input_identity.identity_digest(),
        evidence.durable_dataset.snapshot_digest,
        evidence.durable_dataset.feature_membership_digest,
        evidence.ordered_entry_digest,
    ]
    for hardware_item in evidence.hardware:
        values.append(hardware_item.snapshot_ref.digest)
        values.extend(hardware_item.link_digests)
    for calibration_item in evidence.calibrations:
        values.append(calibration_item.calibration_ref.digest)
        values.extend(calibration_item.source_digests)
    for ephemeris_item in evidence.ephemerides:
        values.extend((ephemeris_item.raw_digest, ephemeris_item.normalized_digest))
        values.extend(ephemeris_item.link_digests)
        values.extend(ephemeris_item.selection_policy_digests)
    values.extend(item.digest for item in evidence.carrier_hypothesis_refs)
    values.extend(item.digest for item in evidence.prediction_policy_refs)
    return tuple(values)


def tracking_model_dependency_digests(
    evidence: TrackingModelEvidence,
) -> tuple[Digest, ...]:
    return tuple(
        item.digest
        for item in (
            evidence.algorithm_ref,
            evidence.config_ref,
            evidence.propagator_ref,
            evidence.gravity_model_ref,
            evidence.time_scale_ref,
            evidence.earth_orientation_ref,
            evidence.error_policy_ref,
        )
    )


def tracking_model_snapshot_digest(
    schema: SchemaRef,
    evidence: TrackingModelEvidence,
    receivers: tuple[ReceiverLnbFrequencyEstimate, ...],
    satellites: tuple[SatelliteCarrierResidualEstimate, ...],
    joint_covariance: Covariance,
    accepted: tuple[AcceptedAssociationEvidence, ...],
    rejected: tuple[RejectedAssociationEvidence, ...],
    warnings: tuple[str, ...],
) -> Digest:
    return canonical_digest(
        {
            "schema": schema,
            "evidence": evidence,
            "receiver_lnb_estimates": receivers,
            "satellite_carrier_residual_estimates": satellites,
            "joint_covariance": joint_covariance,
            "accepted_associations": accepted,
            "rejected_associations": rejected,
            "warnings": warnings,
        }
    )


def _estimate(
    value: tuple[float, float],
    basis: tuple[str, str],
    units: tuple[str, str],
    covariance: Covariance,
    expected_basis: tuple[str, str],
    count: int,
    name: str,
) -> None:
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 0 < count <= MAX_ASSOCIATION_OUTCOMES
    ):
        raise ValueError(f"{name} observation count is invalid")
    if (
        basis != expected_basis
        or units != RF_UNITS
        or covariance.basis != basis
        or covariance.units != units
    ):
        raise ValueError(f"{name} basis or units differ")
    for item in value:
        require_finite(item, f"{name} value")
    if len(covariance.values) != 2 or any(len(row) != 2 for row in covariance.values):
        raise ValueError(f"{name} covariance must be full 2x2")
    if covariance.psd_tolerance != JOINT_COVARIANCE_PSD_TOLERANCE:
        raise ValueError(f"{name} covariance tolerance differs")
    _exact_symmetric(covariance, f"{name} covariance")
    if covariance.values[0][0] < 0.0 or covariance.values[1][1] < 0.0:
        raise ValueError(f"{name} covariance diagonal must be nonnegative")


def _exact_symmetric(covariance: Covariance, name: str) -> None:
    for row in range(len(covariance.values)):
        for column in range(row + 1, len(covariance.values)):
            if covariance.values[row][column] != covariance.values[column][row]:
                raise ValueError(f"{name} must be exactly symmetric")


def _schema_artifact(ref: ArtifactRef, name: str) -> None:
    if ref.schema is None:
        raise ValueError(f"{name} requires a schema")


def _canonical_artifacts(refs: tuple[ArtifactRef, ...], name: str) -> None:
    if not refs or refs != tuple(sorted(refs, key=_artifact_key)):
        raise ValueError(f"{name} must be non-empty and canonical")
    if len({item.artifact_id for item in refs}) != len(refs):
        raise ValueError(f"{name} IDs are duplicated")
    for ref in refs:
        _schema_artifact(ref, name)


def _canonical_digests(values: tuple[Digest, ...], name: str) -> None:
    if not values or values != tuple(sorted(set(values), key=str)):
        raise ValueError(f"{name} must be non-empty, unique, and canonical")


def _unique_grouped_digests(groups: tuple[tuple[Digest, ...], ...], name: str) -> None:
    flattened = tuple(item for group in groups for item in group)
    if len(set(flattened)) != len(flattened):
        raise ValueError(f"{name} are duplicated")


def _norad_id(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("NORAD ID must be a positive integer")


def _carrier_candidate_key(
    item: CarrierAssociationCandidate,
) -> tuple[int, str, str]:
    return item.norad_id, *_artifact_key(item.carrier_hypothesis_ref)


def _carrier_candidates(values: tuple[CarrierAssociationCandidate, ...]) -> None:
    if not values or values != tuple(sorted(set(values), key=_carrier_candidate_key)):
        raise ValueError("carrier candidates must be non-empty, unique, and canonical")


def _entry_index(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("entry_index must be a nonnegative integer")


def _contains(interval: RecordingInterval, value: UtcNs) -> bool:
    return interval.started_utc_ns <= value < interval.finished_utc_ns


def _artifact_key(ref: ArtifactRef) -> tuple[str, str]:
    return ref.artifact_id, str(ref.digest)


def _hardware_key(item: TrackingHardwareEvidence) -> tuple[str, str, str]:
    return (
        str(item.snapshot_ref.snapshot_id),
        str(item.snapshot_ref.digest),
        str(canonical_digest(item.link_digests)),
    )


def _calibration_key(item: TrackingCalibrationEvidence) -> tuple[str, str]:
    return _artifact_key(item.calibration_ref)


def _ephemeris_key(item: TrackingEphemerisEvidence) -> tuple[str, str, str]:
    return item.source.value, str(item.snapshot_id), str(item.normalized_digest)


def _receiver_key(item: ReceiverLnbFrequencyEstimate) -> tuple[str, int, int, str]:
    return (
        str(item.receiver_chain_id),
        int(item.validity.started_utc_ns),
        int(item.validity.finished_utc_ns),
        str(item.hardware_snapshot_ref.digest),
    )


def _satellite_key(
    item: SatelliteCarrierResidualEstimate,
) -> tuple[int, str, str, int, int]:
    return (
        item.norad_id,
        *_artifact_key(item.carrier_hypothesis_ref),
        int(item.validity.started_utc_ns),
        int(item.validity.finished_utc_ns),
    )
