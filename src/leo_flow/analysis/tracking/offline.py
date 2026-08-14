"""Experimental, offline filtering of residual RF association measurements.

This module tracks measurement residuals for one already-associated NORAD
hypothesis.  It does not estimate an orbit and must not be used as evidence of
satellite identity.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite

from leo_flow.analysis.orbit import (
    AssociationCandidate,
    AssociationDecision,
    AssociationStatus,
    RfAssociationRequest,
)
from leo_flow.contracts._validation import require_nonnegative, require_positive
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    ReceiverChainId,
    RecordingId,
    StationId,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.hardware import HardwareMetadataSnapshotRef

Vector2 = tuple[float, float]
Matrix2 = tuple[tuple[float, float], tuple[float, float]]

STATE_BASIS = ("frequency_residual_hz", "frequency_drift_residual_hz_s")


@dataclass(frozen=True)
class TrackingSpecification:
    """Pinned choices for one bounded residual-filter experiment."""

    specification_ref: ArtifactRef
    expected_norad_id: int
    initial_state: Vector2
    initial_covariance: Matrix2
    process_noise_hz2_s3: float
    maximum_normalized_innovation_squared: float
    maximum_gap_s: float
    segment_on_context_change: bool = True
    segment_on_excessive_gap: bool = True
    enable_rts_smoothing: bool = True

    def __post_init__(self) -> None:
        if self.expected_norad_id <= 0:
            raise ValueError("expected NORAD ID must be positive")
        _require_vector(self.initial_state, "initial_state")
        _require_positive_definite(self.initial_covariance, "initial_covariance")
        require_nonnegative(self.process_noise_hz2_s3, "process_noise_hz2_s3")
        require_positive(
            self.maximum_normalized_innovation_squared,
            "maximum_normalized_innovation_squared",
        )
        require_positive(self.maximum_gap_s, "maximum_gap_s")


@dataclass(frozen=True)
class AssociatedTrackingObservation:
    """One association plus the uncertainty absent from its point prediction.

    Prediction variances include carrier, calibration, propagation, and other
    uncertainty not already included in the measurement variances.
    """

    decision: AssociationDecision
    request: RfAssociationRequest
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
        if self.decision.request_digest != canonical_digest(self.request):
            raise ValueError("association decision and exact request digest differ")


@dataclass(frozen=True)
class TrackingContext:
    receiver_chain_id: ReceiverChainId
    hardware_snapshot_ref: HardwareMetadataSnapshotRef
    station_id: StationId
    station_geometry_digest: Digest


@dataclass(frozen=True)
class TrackingEstimate:
    recording_id: RecordingId
    utc_ns: UtcNs
    state_basis: tuple[str, str]
    filtered_state: Vector2
    filtered_covariance: Matrix2
    predicted_state: Vector2
    predicted_covariance: Matrix2
    innovation: Vector2
    innovation_covariance: Matrix2
    normalized_innovation_squared: float
    smoothed_state: Vector2 | None = None
    smoothed_covariance: Matrix2 | None = None


@dataclass(frozen=True)
class RejectedTrackingObservation:
    recording_id: RecordingId
    utc_ns: UtcNs
    reason_code: str


@dataclass(frozen=True)
class TrackSegment:
    segment_id: str
    norad_id: int
    context: TrackingContext
    start_reason: str
    termination_reason: str
    estimates: tuple[TrackingEstimate, ...]


@dataclass(frozen=True)
class TrackingReport:
    experimental: bool
    software_truth_only: bool
    specification_ref: ArtifactRef
    expected_norad_id: int
    input_digest: Digest
    segments: tuple[TrackSegment, ...]
    rejected: tuple[RejectedTrackingObservation, ...]


@dataclass
class _Step:
    estimate: TrackingEstimate
    transition_from_previous: Matrix2


def track_associated_observations(
    observations: tuple[AssociatedTrackingObservation, ...],
    specification: TrackingSpecification,
) -> TrackingReport:
    """Filter ordered observations, never changing the fixed NORAD identity."""

    previous_input_time: int | None = None
    segments: list[TrackSegment] = []
    rejected: list[RejectedTrackingObservation] = []
    steps: list[_Step] = []
    context: TrackingContext | None = None
    start_reason = "first-accepted-observation"
    state = specification.initial_state
    covariance = specification.initial_covariance
    previous_accepted_time: int | None = None

    def finish(reason: str) -> None:
        nonlocal steps, context, state, covariance, previous_accepted_time
        if not steps or context is None:
            return
        estimates = (
            _smooth(steps)
            if specification.enable_rts_smoothing
            else tuple(step.estimate for step in steps)
        )
        identity = {
            "norad_id": specification.expected_norad_id,
            "specification_ref": specification.specification_ref,
            "context": context,
            "estimates": estimates,
        }
        digest = canonical_digest(identity)
        segments.append(
            TrackSegment(
                f"trackseg_{digest.value[:32]}",
                specification.expected_norad_id,
                context,
                start_reason,
                reason,
                estimates,
            )
        )
        steps = []
        context = None
        state = specification.initial_state
        covariance = specification.initial_covariance
        previous_accepted_time = None

    for observation in observations:
        measurement = observation.request.measurement
        time_ns = int(measurement.midpoint_utc_ns)
        if previous_input_time is not None and time_ns <= previous_input_time:
            raise ValueError("tracking observations must have strictly increasing time")
        previous_input_time = time_ns

        rejection = _association_rejection(observation, specification.expected_norad_id)
        if rejection is not None:
            finish(f"rejected:{rejection}")
            rejected.append(
                RejectedTrackingObservation(
                    measurement.recording_id,
                    measurement.midpoint_utc_ns,
                    rejection,
                )
            )
            start_reason = f"after-rejected:{rejection}"
            continue

        candidate = _selected_candidate(observation.decision)
        next_context = _context(observation)
        if previous_accepted_time is not None:
            gap_s = (time_ns - previous_accepted_time) / 1_000_000_000.0
            if gap_s > specification.maximum_gap_s:
                if specification.segment_on_excessive_gap:
                    finish("excessive-gap")
                    start_reason = "after-excessive-gap"
                else:
                    finish("rejected:excessive-gap")
                    rejected.append(
                        RejectedTrackingObservation(
                            measurement.recording_id,
                            measurement.midpoint_utc_ns,
                            "excessive-gap",
                        )
                    )
                    start_reason = "after-rejected:excessive-gap"
                    continue
        if context is not None and next_context != context:
            if specification.segment_on_context_change:
                finish("context-change")
                start_reason = "after-context-change"
            else:
                finish("rejected:context-change")
                rejected.append(
                    RejectedTrackingObservation(
                        measurement.recording_id,
                        measurement.midpoint_utc_ns,
                        "context-change",
                    )
                )
                start_reason = "after-rejected:context-change"
                continue
        if context is None:
            context = next_context

        dt_s = (
            0.0
            if previous_accepted_time is None
            else (time_ns - previous_accepted_time) / 1_000_000_000.0
        )
        transition = ((1.0, dt_s), (0.0, 1.0))
        predicted_state = _matvec(transition, state)
        predicted_covariance = _add(
            _matmul(_matmul(transition, covariance), _transpose(transition)),
            _process_covariance(specification.process_noise_hz2_s3, dt_s),
        )
        measured_residual = (
            measurement.frequency_hz - candidate.predicted_frequency_hz,
            measurement.drift_hz_s - candidate.predicted_drift_hz_s,
        )
        measurement_covariance = (
            (
                measurement.frequency_variance_hz2
                + observation.prediction_frequency_variance_hz2,
                0.0,
            ),
            (
                0.0,
                measurement.drift_variance_hz2_s2
                + observation.prediction_drift_variance_hz2_s2,
            ),
        )
        innovation = _sub_vector(measured_residual, predicted_state)
        innovation_covariance = _add(predicted_covariance, measurement_covariance)
        innovation_inverse = _inverse(innovation_covariance)
        nis = _quadratic(innovation, innovation_inverse)
        if nis > specification.maximum_normalized_innovation_squared:
            finish("rejected:innovation-gate")
            rejected.append(
                RejectedTrackingObservation(
                    measurement.recording_id,
                    measurement.midpoint_utc_ns,
                    "innovation-gate",
                )
            )
            start_reason = "after-rejected:innovation-gate"
            continue

        gain = _matmul(predicted_covariance, innovation_inverse)
        filtered_state = _add_vector(predicted_state, _matvec(gain, innovation))
        identity = ((1.0, 0.0), (0.0, 1.0))
        residual_gain = _sub(identity, gain)
        # Joseph form retains symmetry and positive semidefiniteness better.
        filtered_covariance = _add(
            _matmul(
                _matmul(residual_gain, predicted_covariance),
                _transpose(residual_gain),
            ),
            _matmul(_matmul(gain, measurement_covariance), _transpose(gain)),
        )
        filtered_covariance = _symmetrize(filtered_covariance)
        _require_positive_definite(filtered_covariance, "filtered_covariance")
        estimate = TrackingEstimate(
            measurement.recording_id,
            measurement.midpoint_utc_ns,
            STATE_BASIS,
            filtered_state,
            filtered_covariance,
            predicted_state,
            predicted_covariance,
            innovation,
            innovation_covariance,
            nis,
        )
        steps.append(_Step(estimate, transition))
        state = filtered_state
        covariance = filtered_covariance
        previous_accepted_time = time_ns

    finish("end-of-input")
    return TrackingReport(
        True,
        True,
        specification.specification_ref,
        specification.expected_norad_id,
        canonical_digest(observations),
        tuple(segments),
        tuple(rejected),
    )


def _association_rejection(
    observation: AssociatedTrackingObservation, expected_norad_id: int
) -> str | None:
    decision = observation.decision
    if any(code.startswith("propagation-error:") for code in decision.reason_codes):
        return "propagation-error"
    if decision.status is AssociationStatus.AMBIGUOUS:
        return "ambiguous-association"
    if decision.status is AssociationStatus.NO_MATCH:
        return "no-match-association"
    if decision.status is not AssociationStatus.MATCH:
        return "unsupported-association-status"
    if decision.selected_norad_id != expected_norad_id:
        return "norad-identity-mismatch"
    selected = [
        candidate
        for candidate in decision.candidates
        if candidate.norad_id == decision.selected_norad_id
    ]
    if len(selected) != 1:
        return "malformed-association-decision"
    candidate = selected[0]
    if (
        not decision.candidates
        or decision.candidates[0] != candidate
        or not all(
            isfinite(value)
            for value in (
                candidate.normalized_squared_residual,
                candidate.predicted_frequency_hz,
                candidate.predicted_drift_hz_s,
                candidate.elevation_deg,
            )
        )
    ):
        return "malformed-association-decision"
    return None


def _selected_candidate(decision: AssociationDecision) -> AssociationCandidate:
    candidates = [
        candidate
        for candidate in decision.candidates
        if candidate.norad_id == decision.selected_norad_id
    ]
    if len(candidates) != 1:
        raise ValueError("association decision has no unique selected candidate")
    return candidates[0]


def _context(observation: AssociatedTrackingObservation) -> TrackingContext:
    request = observation.request
    return TrackingContext(
        request.measurement.receiver_chain_id,
        request.calibration.hardware_snapshot_ref,
        request.station.station_id,
        request.station.digest,
    )


def _process_covariance(noise: float, dt_s: float) -> Matrix2:
    return (
        (noise * dt_s**3 / 3.0, noise * dt_s**2 / 2.0),
        (noise * dt_s**2 / 2.0, noise * dt_s),
    )


def _smooth(steps: list[_Step]) -> tuple[TrackingEstimate, ...]:
    if not steps:
        return ()
    result = [replace(step.estimate) for step in steps]
    last = result[-1]
    result[-1] = replace(
        last,
        smoothed_state=last.filtered_state,
        smoothed_covariance=last.filtered_covariance,
    )
    for index in range(len(result) - 2, -1, -1):
        current = result[index]
        following = result[index + 1]
        following_step = steps[index + 1]
        assert following.smoothed_state is not None
        assert following.smoothed_covariance is not None
        smoother_gain = _matmul(
            _matmul(
                current.filtered_covariance,
                _transpose(following_step.transition_from_previous),
            ),
            _inverse(following.predicted_covariance),
        )
        smoothed_state = _add_vector(
            current.filtered_state,
            _matvec(
                smoother_gain,
                _sub_vector(following.smoothed_state, following.predicted_state),
            ),
        )
        smoothed_covariance = _add(
            current.filtered_covariance,
            _matmul(
                _matmul(
                    smoother_gain,
                    _sub(
                        following.smoothed_covariance,
                        following.predicted_covariance,
                    ),
                ),
                _transpose(smoother_gain),
            ),
        )
        smoothed_covariance = _symmetrize(smoothed_covariance)
        _require_positive_definite(smoothed_covariance, "smoothed_covariance")
        result[index] = replace(
            current,
            smoothed_state=smoothed_state,
            smoothed_covariance=smoothed_covariance,
        )
    return tuple(result)


def _require_vector(value: Vector2, name: str) -> None:
    if len(value) != 2 or not all(isfinite(item) for item in value):
        raise ValueError(f"{name} must contain two finite values")


def _require_positive_definite(value: Matrix2, name: str) -> None:
    if (
        len(value) != 2
        or any(len(row) != 2 for row in value)
        or not all(isfinite(item) for row in value for item in row)
        or abs(value[0][1] - value[1][0]) > 1e-9
        or value[0][0] <= 0.0
        or value[1][1] <= 0.0
        or _determinant(value) <= 0.0
    ):
        raise ValueError(f"{name} must be finite, symmetric, and positive definite")


def _determinant(value: Matrix2) -> float:
    return value[0][0] * value[1][1] - value[0][1] * value[1][0]


def _inverse(value: Matrix2) -> Matrix2:
    determinant = _determinant(value)
    if not isfinite(determinant) or determinant <= 0.0:
        raise ValueError("matrix must be positive definite and invertible")
    return (
        (value[1][1] / determinant, -value[0][1] / determinant),
        (-value[1][0] / determinant, value[0][0] / determinant),
    )


def _transpose(value: Matrix2) -> Matrix2:
    return ((value[0][0], value[1][0]), (value[0][1], value[1][1]))


def _matmul(left: Matrix2, right: Matrix2) -> Matrix2:
    return (
        (
            left[0][0] * right[0][0] + left[0][1] * right[1][0],
            left[0][0] * right[0][1] + left[0][1] * right[1][1],
        ),
        (
            left[1][0] * right[0][0] + left[1][1] * right[1][0],
            left[1][0] * right[0][1] + left[1][1] * right[1][1],
        ),
    )


def _matvec(matrix: Matrix2, vector: Vector2) -> Vector2:
    return (
        matrix[0][0] * vector[0] + matrix[0][1] * vector[1],
        matrix[1][0] * vector[0] + matrix[1][1] * vector[1],
    )


def _add(left: Matrix2, right: Matrix2) -> Matrix2:
    return (
        (left[0][0] + right[0][0], left[0][1] + right[0][1]),
        (left[1][0] + right[1][0], left[1][1] + right[1][1]),
    )


def _sub(left: Matrix2, right: Matrix2) -> Matrix2:
    return (
        (left[0][0] - right[0][0], left[0][1] - right[0][1]),
        (left[1][0] - right[1][0], left[1][1] - right[1][1]),
    )


def _add_vector(left: Vector2, right: Vector2) -> Vector2:
    return (left[0] + right[0], left[1] + right[1])


def _sub_vector(left: Vector2, right: Vector2) -> Vector2:
    return (left[0] - right[0], left[1] - right[1])


def _quadratic(vector: Vector2, matrix: Matrix2) -> float:
    product = _matvec(matrix, vector)
    return vector[0] * product[0] + vector[1] * product[1]


def _symmetrize(value: Matrix2) -> Matrix2:
    off_diagonal = (value[0][1] + value[1][0]) / 2.0
    return ((value[0][0], off_diagonal), (off_diagonal, value[1][1]))
