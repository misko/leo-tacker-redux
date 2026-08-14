"""Independent deterministic observation source for tracking experiments."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from math import isfinite

from leo_flow.contracts._validation import require_nonnegative
from leo_flow.contracts.core import ArtifactRef, canonical_digest

from .offline import AssociatedTrackingObservation


@dataclass(frozen=True)
class SyntheticTrackingSpecification:
    specification_ref: ArtifactRef
    seed: int
    initial_frequency_residual_hz: float
    initial_drift_residual_hz_s: float
    drift_acceleration_hz_s2: float
    frequency_noise_span_hz: float
    drift_noise_span_hz_s: float

    def __post_init__(self) -> None:
        if self.seed < 0:
            raise ValueError("synthetic seed must be nonnegative")
        for value in (
            self.initial_frequency_residual_hz,
            self.initial_drift_residual_hz_s,
            self.drift_acceleration_hz_s2,
        ):
            if not isfinite(value):
                raise ValueError("synthetic truth values must be finite")
        require_nonnegative(self.frequency_noise_span_hz, "frequency_noise_span_hz")
        require_nonnegative(self.drift_noise_span_hz_s, "drift_noise_span_hz_s")


def inject_synthetic_tracking_observation(
    template: AssociatedTrackingObservation,
    specification: SyntheticTrackingSpecification,
    *,
    elapsed_s: float,
    case_key: str,
) -> AssociatedTrackingObservation:
    """Inject software truth without calling the filtering implementation."""

    if not isfinite(elapsed_s) or elapsed_s < 0.0:
        raise ValueError("elapsed_s must be finite and nonnegative")
    if not case_key or any(character.isspace() for character in case_key):
        raise ValueError("case_key must be a token")
    selected = [
        candidate
        for candidate in template.decision.candidates
        if candidate.norad_id == template.decision.selected_norad_id
    ]
    if len(selected) != 1:
        raise ValueError("template must contain one selected candidate")
    candidate = selected[0]
    frequency_truth = (
        specification.initial_frequency_residual_hz
        + specification.initial_drift_residual_hz_s * elapsed_s
        + 0.5 * specification.drift_acceleration_hz_s2 * elapsed_s**2
    )
    drift_truth = (
        specification.initial_drift_residual_hz_s
        + specification.drift_acceleration_hz_s2 * elapsed_s
    )
    frequency_noise = specification.frequency_noise_span_hz * _noise(
        specification.seed, case_key, "frequency"
    )
    drift_noise = specification.drift_noise_span_hz_s * _noise(
        specification.seed, case_key, "drift"
    )
    request = replace(
        template.request,
        measurement=replace(
            template.request.measurement,
            frequency_hz=candidate.predicted_frequency_hz
            + frequency_truth
            + frequency_noise,
            drift_hz_s=candidate.predicted_drift_hz_s + drift_truth + drift_noise,
        ),
    )
    return replace(
        template,
        request=request,
        decision=replace(template.decision, request_digest=canonical_digest(request)),
    )


def synthetic_truth_state(
    specification: SyntheticTrackingSpecification, elapsed_s: float
) -> tuple[float, float]:
    """Return the exact injected state for validation reporting."""

    if not isfinite(elapsed_s) or elapsed_s < 0.0:
        raise ValueError("elapsed_s must be finite and nonnegative")
    return (
        specification.initial_frequency_residual_hz
        + specification.initial_drift_residual_hz_s * elapsed_s
        + 0.5 * specification.drift_acceleration_hz_s2 * elapsed_s**2,
        specification.initial_drift_residual_hz_s
        + specification.drift_acceleration_hz_s2 * elapsed_s,
    )


def _noise(seed: int, case_key: str, axis: str) -> float:
    raw = hashlib.sha256(f"{seed}:{case_key}:{axis}".encode()).digest()
    integer = int.from_bytes(raw[:8], "big")
    return 2.0 * ((integer + 0.5) / 2**64) - 1.0
