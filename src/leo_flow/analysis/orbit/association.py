"""Dependency-free scientific contract and deterministic RF association core."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from leo_flow.contracts._validation import (
    require_finite,
    require_nonnegative,
    require_positive,
    require_utc_ns,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    FeatureId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
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
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.contracts.hardware import HardwareMetadataSnapshotRef


@dataclass(frozen=True)
class PropagationSpecification:
    """Every non-TLE choice that can alter an orbit prediction."""

    propagator_ref: ArtifactRef
    gravity_model_ref: ArtifactRef
    time_scale_ref: ArtifactRef
    earth_orientation_ref: ArtifactRef
    error_policy_ref: ArtifactRef
    speed_of_light_m_s: float = 299_792_458.0

    def __post_init__(self) -> None:
        require_positive(self.speed_of_light_m_s, "speed_of_light_m_s")


@dataclass(frozen=True)
class StationGeometrySnapshot:
    station_id: StationId
    frame: str
    position_m: tuple[float, float, float]
    digest: Digest

    def __post_init__(self) -> None:
        if self.frame != "ITRF":
            raise ValueError(
                "only an explicitly pinned ITRF station frame is supported"
            )
        for value in self.position_m:
            require_finite(value, "station position")
        if self.digest != canonical_digest(
            {
                "station_id": str(self.station_id),
                "frame": self.frame,
                "position_m": self.position_m,
            }
        ):
            raise ValueError("station geometry digest differs")


@dataclass(frozen=True)
class ReceiverRfCalibration:
    receiver_chain_id: ReceiverChainId
    hardware_snapshot_ref: HardwareMetadataSnapshotRef
    station_id: StationId
    frequency_bias_hz: float
    frequency_drift_hz_s: float
    frequency_variance_hz2: float
    drift_variance_hz2_s2: float

    def __post_init__(self) -> None:
        require_finite(self.frequency_bias_hz, "frequency_bias_hz")
        require_finite(self.frequency_drift_hz_s, "frequency_drift_hz_s")
        require_nonnegative(self.frequency_variance_hz2, "frequency_variance_hz2")
        require_nonnegative(self.drift_variance_hz2_s2, "drift_variance_hz2_s2")


@dataclass(frozen=True)
class SatelliteCarrierHypothesis:
    norad_id: int
    carrier_hz: float
    carrier_variance_hz2: float

    def __post_init__(self) -> None:
        if self.norad_id <= 0:
            raise ValueError("NORAD ID must be positive")
        require_positive(self.carrier_hz, "carrier_hz")
        require_nonnegative(self.carrier_variance_hz2, "carrier_variance_hz2")


@dataclass(frozen=True)
class RfMeasurement:
    feature_set_ref: FeatureSetRef
    feature_id: FeatureId
    recording_id: RecordingId
    receiver_chain_id: ReceiverChainId
    midpoint_utc_ns: UtcNs
    frequency_hz: float
    drift_hz_s: float
    frequency_variance_hz2: float
    drift_variance_hz2_s2: float

    def __post_init__(self) -> None:
        require_utc_ns(self.midpoint_utc_ns, "midpoint_utc_ns")
        require_finite(self.frequency_hz, "frequency_hz")
        require_finite(self.drift_hz_s, "drift_hz_s")
        require_positive(self.frequency_variance_hz2, "frequency_variance_hz2")
        require_positive(self.drift_variance_hz2_s2, "drift_variance_hz2_s2")


@dataclass(frozen=True)
class PropagatedState:
    norad_id: int
    utc_ns: UtcNs
    range_rate_m_s: float
    range_acceleration_m_s2: float
    elevation_deg: float
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.norad_id <= 0:
            raise ValueError("NORAD ID must be positive")
        require_utc_ns(self.utc_ns, "utc_ns")
        for value in (
            self.range_rate_m_s,
            self.range_acceleration_m_s2,
            self.elevation_deg,
        ):
            require_finite(value, "propagated state")
        if not -90.0 <= self.elevation_deg <= 90.0:
            raise ValueError("elevation must lie in [-90, 90] degrees")


class OrbitPropagator(Protocol):
    """Offline adapter seam; a production SGP4 implementation lives behind it."""

    def propagate(
        self,
        snapshot_ref: EphemerisSnapshotRef,
        station: StationGeometrySnapshot,
        specification: PropagationSpecification,
        norad_id: int,
        utc_ns: UtcNs,
    ) -> PropagatedState: ...


class DeterministicOrbitSimulator:
    """Exact state lookup for tests and synthetic association experiments."""

    def __init__(self, states: tuple[PropagatedState, ...]) -> None:
        self._states = {(state.norad_id, state.utc_ns): state for state in states}
        if len(self._states) != len(states):
            raise ValueError("simulated orbit state keys must be unique")

    def propagate(
        self,
        snapshot_ref: EphemerisSnapshotRef,
        station: StationGeometrySnapshot,
        specification: PropagationSpecification,
        norad_id: int,
        utc_ns: UtcNs,
    ) -> PropagatedState:
        del snapshot_ref, station, specification
        try:
            return self._states[(norad_id, utc_ns)]
        except KeyError as error:
            raise LookupError("simulator has no exact orbit state") from error


@dataclass(frozen=True)
class AssociationPolicy:
    policy_ref: ArtifactRef
    minimum_elevation_deg: float
    maximum_normalized_squared_residual: float
    ambiguity_delta: float
    tie_tolerance: float = 1e-12

    def __post_init__(self) -> None:
        require_finite(self.minimum_elevation_deg, "minimum_elevation_deg")
        require_positive(
            self.maximum_normalized_squared_residual,
            "maximum_normalized_squared_residual",
        )
        require_nonnegative(self.ambiguity_delta, "ambiguity_delta")
        require_nonnegative(self.tie_tolerance, "tie_tolerance")


@dataclass(frozen=True)
class EphemerisLinkEvidence:
    """Full immutable link projection, verified against its artifact identity."""

    link_ref: ArtifactRef
    recording_id: RecordingId
    recording_identity_digest: Digest
    recording_interval: RecordingInterval
    source: EphemerisSource
    scope: str
    selection_policy: EphemerisSelectionPolicy
    selection_policy_ref: ArtifactRef
    as_of_utc_ns: UtcNs
    snapshot_ref: EphemerisSnapshotRef

    def __post_init__(self) -> None:
        require_utc_ns(self.as_of_utc_ns, "as_of_utc_ns")
        if not self.scope or any(character.isspace() for character in self.scope):
            raise ValueError("scope must be a token")
        if self.selection_policy is EphemerisSelectionPolicy.BEST_EPHEMERIS:
            raise ValueError("best_ephemeris has no frozen selection semantics")
        if self.snapshot_ref.source is not self.source:
            raise ValueError("ephemeris link cannot cross providers")
        identity = {
            "recording_identity_digest": str(self.recording_identity_digest),
            "recording_interval": self.recording_interval,
            "source": self.source.value,
            "scope": self.scope,
            "policy": self.selection_policy.value,
            "policy_ref": self.selection_policy_ref,
            "as_of_utc_ns": self.as_of_utc_ns,
            "snapshot_ref": self.snapshot_ref,
        }
        expected = canonical_digest(identity)
        if (
            self.link_ref.schema != SchemaRef("org.leo-flow.recording-ephemeris-link")
            or self.link_ref.digest != expected
            or self.link_ref.artifact_id != f"ephlink_{expected.value[:32]}"
        ):
            raise ValueError("ephemeris link artifact identity differs")


@dataclass(frozen=True)
class RfAssociationRequest:
    ephemeris_link: EphemerisLinkEvidence
    station: StationGeometrySnapshot
    propagation: PropagationSpecification
    measurement: RfMeasurement
    calibration: ReceiverRfCalibration
    carriers: tuple[SatelliteCarrierHypothesis, ...]
    policy: AssociationPolicy

    def __post_init__(self) -> None:
        if not self.carriers:
            raise ValueError("association requires carrier hypotheses")
        if len({item.norad_id for item in self.carriers}) != len(self.carriers):
            raise ValueError("carrier hypotheses must have unique NORAD IDs")
        if self.measurement.receiver_chain_id != self.calibration.receiver_chain_id:
            raise ValueError("measurement and RF calibration chains differ")
        if self.measurement.recording_id != self.ephemeris_link.recording_id:
            raise ValueError("feature recording and ephemeris link differ")
        if not (
            self.ephemeris_link.recording_interval.started_utc_ns
            <= self.measurement.midpoint_utc_ns
            <= self.ephemeris_link.recording_interval.finished_utc_ns
        ):
            raise ValueError("measurement time lies outside linked recording")
        if self.station.station_id != self.calibration.station_id:
            raise ValueError("station geometry and hardware station differ")


@dataclass(frozen=True)
class AssociationCandidate:
    norad_id: int
    normalized_squared_residual: float
    predicted_frequency_hz: float
    predicted_drift_hz_s: float
    elevation_deg: float


class AssociationStatus(str, Enum):
    MATCH = "match"
    AMBIGUOUS = "ambiguous"
    NO_MATCH = "no_match"


@dataclass(frozen=True)
class AssociationDecision:
    status: AssociationStatus
    selected_norad_id: int | None
    candidates: tuple[AssociationCandidate, ...]
    request_digest: Digest
    reason_codes: tuple[str, ...]


def associate_rf_measurement(
    request: RfAssociationRequest, propagator: OrbitPropagator
) -> AssociationDecision:
    """Score hypotheses without promoting an association to scientific truth."""

    candidates: list[AssociationCandidate] = []
    reasons: list[str] = []
    measurement = request.measurement
    calibration = request.calibration
    c = request.propagation.speed_of_light_m_s
    for carrier in sorted(request.carriers, key=lambda item: item.norad_id):
        state = propagator.propagate(
            request.ephemeris_link.snapshot_ref,
            request.station,
            request.propagation,
            carrier.norad_id,
            measurement.midpoint_utc_ns,
        )
        if (
            state.norad_id != carrier.norad_id
            or state.utc_ns != measurement.midpoint_utc_ns
        ):
            raise ValueError("propagator substituted state identity")
        if state.error_code is not None:
            reasons.append(f"propagation-error:{carrier.norad_id}:{state.error_code}")
            continue
        if state.elevation_deg < request.policy.minimum_elevation_deg:
            reasons.append(f"below-elevation-gate:{carrier.norad_id}")
            continue
        predicted_frequency = (
            carrier.carrier_hz * (1.0 - state.range_rate_m_s / c)
            + calibration.frequency_bias_hz
        )
        predicted_drift = (
            -carrier.carrier_hz * state.range_acceleration_m_s2 / c
            + calibration.frequency_drift_hz_s
        )
        frequency_variance = (
            measurement.frequency_variance_hz2
            + calibration.frequency_variance_hz2
            + carrier.carrier_variance_hz2
        )
        drift_variance = (
            measurement.drift_variance_hz2_s2 + calibration.drift_variance_hz2_s2
        )
        score = (
            measurement.frequency_hz - predicted_frequency
        ) ** 2 / frequency_variance + (
            measurement.drift_hz_s - predicted_drift
        ) ** 2 / drift_variance
        if score <= request.policy.maximum_normalized_squared_residual:
            candidates.append(
                AssociationCandidate(
                    carrier.norad_id,
                    score,
                    predicted_frequency,
                    predicted_drift,
                    state.elevation_deg,
                )
            )
        else:
            reasons.append(f"residual-gate:{carrier.norad_id}")
    candidates.sort(key=lambda item: (item.normalized_squared_residual, item.norad_id))
    digest = canonical_digest(request)
    if not candidates:
        return AssociationDecision(
            AssociationStatus.NO_MATCH, None, (), digest, tuple(reasons)
        )
    if len(candidates) > 1 and (
        candidates[1].normalized_squared_residual
        - candidates[0].normalized_squared_residual
        <= request.policy.ambiguity_delta + request.policy.tie_tolerance
    ):
        return AssociationDecision(
            AssociationStatus.AMBIGUOUS,
            None,
            tuple(candidates),
            digest,
            tuple(reasons) + ("ambiguous-best-candidates",),
        )
    return AssociationDecision(
        AssociationStatus.MATCH,
        candidates[0].norad_id,
        tuple(candidates),
        digest,
        tuple(reasons),
    )
