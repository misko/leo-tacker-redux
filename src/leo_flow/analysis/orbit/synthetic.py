"""Independent deterministic RF measurement injection for bounded experiments.

This module deliberately does not import or call the association scorer.  It
implements a small digital-injection oracle so experiments do not synthesize
their measurements with the function they are evaluating.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from leo_flow.contracts._validation import require_nonnegative, require_positive
from leo_flow.contracts.core import ArtifactRef

from .association import (
    PropagatedState,
    ReceiverRfCalibration,
    RfMeasurement,
    SatelliteCarrierHypothesis,
)


@dataclass(frozen=True)
class DigitalInjectionSpecification:
    """All choices made by the deterministic synthetic RF source."""

    specification_ref: ArtifactRef
    seed: int
    frequency_noise_span_hz: float
    drift_noise_span_hz_s: float
    speed_of_light_m_s: float = 299_792_458.0

    def __post_init__(self) -> None:
        if self.seed < 0:
            raise ValueError("digital-injection seed must be nonnegative")
        require_nonnegative(self.frequency_noise_span_hz, "frequency_noise_span_hz")
        require_nonnegative(self.drift_noise_span_hz_s, "drift_noise_span_hz_s")
        require_positive(self.speed_of_light_m_s, "speed_of_light_m_s")


def inject_synthetic_rf_measurement(
    template: RfMeasurement,
    truth_state: PropagatedState,
    carrier: SatelliteCarrierHypothesis,
    calibration: ReceiverRfCalibration,
    specification: DigitalInjectionSpecification,
    *,
    case_key: str,
    frequency_offset_hz: float = 0.0,
    drift_offset_hz_s: float = 0.0,
) -> RfMeasurement:
    """Return an exact digital injection from pinned state and RF choices.

    The counter-based noise source is stable across Python versions and does
    not depend on call order.  The output is exact synthetic truth only: using
    a TLE-derived ``truth_state`` does not make a satellite identity observed
    ground truth.
    """

    if not case_key or any(character.isspace() for character in case_key):
        raise ValueError("digital-injection case_key must be a token")
    if truth_state.error_code is not None:
        raise ValueError("cannot inject a measurement from a propagation error")
    if truth_state.utc_ns != template.midpoint_utc_ns:
        raise ValueError("truth state and measurement times differ")
    if truth_state.norad_id != carrier.norad_id:
        raise ValueError("truth state and carrier identities differ")
    if template.receiver_chain_id != calibration.receiver_chain_id:
        raise ValueError("measurement and calibration chains differ")

    frequency_noise = specification.frequency_noise_span_hz * _unit_noise(
        specification.seed, case_key, "frequency"
    )
    drift_noise = specification.drift_noise_span_hz_s * _unit_noise(
        specification.seed, case_key, "drift"
    )
    # This is an experiment-owned digital source, intentionally independent of
    # associate_rf_measurement() and any association prediction helper.
    frequency_hz = carrier.carrier_hz + calibration.frequency_bias_hz
    frequency_hz -= (
        carrier.carrier_hz
        * truth_state.range_rate_m_s
        / specification.speed_of_light_m_s
    )
    drift_hz_s = calibration.frequency_drift_hz_s
    drift_hz_s -= (
        carrier.carrier_hz
        * truth_state.range_acceleration_m_s2
        / specification.speed_of_light_m_s
    )
    return replace(
        template,
        frequency_hz=frequency_hz + frequency_noise + frequency_offset_hz,
        drift_hz_s=drift_hz_s + drift_noise + drift_offset_hz_s,
    )


def _unit_noise(seed: int, case_key: str, axis: str) -> float:
    digest = hashlib.sha256(f"{seed}:{case_key}:{axis}".encode()).digest()
    integer = int.from_bytes(digest[:8], "big")
    # The midpoint mapping produces a value strictly inside [-1, 1].
    return 2.0 * ((integer + 0.5) / 2**64) - 1.0
