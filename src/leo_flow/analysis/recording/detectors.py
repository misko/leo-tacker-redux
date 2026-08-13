"""Small deterministic detector kernels for one paired-CI16 window."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .psd import compact_psd


@dataclass(frozen=True)
class PeriodicEvidence:
    score: float
    lag_samples: int
    numerator_magnitude: float
    normalization: float


@dataclass(frozen=True)
class CoarseEnergyEvidence:
    score: float
    peak_bin: int
    frequency_offset_hz: float
    peak_power: float
    median_noise_power: float


@dataclass(frozen=True)
class PairedEvidence:
    score: float
    delay_samples: int
    relative_phase_rad: float
    gain_ratio: float
    differential_power_fraction: float
    conjugate_score: float


def periodic_coherence(
    samples: Sequence[complex], lag_samples: int
) -> PeriodicEvidence:
    """Magnitude-normalized complex autocorrelation at one positive lag."""

    if not 0 < lag_samples < len(samples):
        raise ValueError("periodic lag must lie inside the window")
    left = samples[:-lag_samples]
    right = samples[lag_samples:]
    numerator = sum(a.conjugate() * b for a, b in zip(left, right, strict=True))
    left_energy = math.fsum(abs(value) ** 2 for value in left)
    right_energy = math.fsum(abs(value) ** 2 for value in right)
    normalization = math.sqrt(left_energy * right_energy)
    score = abs(numerator) / normalization if normalization else 0.0
    return PeriodicEvidence(score, lag_samples, abs(numerator), normalization)


def coarse_energy(
    samples: Sequence[complex], *, sample_rate_hz: float, epsilon: float
) -> CoarseEnergyEvidence:
    """Peak-to-median FFT-bin energy after complex-mean removal."""

    summary = compact_psd(
        samples, sample_rate_hz=sample_rate_hz, noise_floor_epsilon=epsilon
    )
    return CoarseEnergyEvidence(
        summary.peak_to_median_ratio,
        summary.peak_bin,
        summary.frequency_offset_hz,
        summary.peak_power,
        summary.median_noise_power,
    )


def paired_common_mode(
    first: Sequence[complex], second: Sequence[complex], *, max_delay_samples: int
) -> PairedEvidence:
    """Find the delay with maximum normalized cross-channel coherence.

    The residual is evaluated after the least-squares complex gain is removed.
    A conjugated-channel coherence is reported as a wiring/representation
    diagnostic, but never substituted for the declared CI16 convention.
    """

    if len(first) != len(second) or not first:
        raise ValueError("paired evidence requires equal non-empty channels")
    if not 0 <= max_delay_samples < len(first):
        raise ValueError("maximum delay must lie inside the window")

    def aligned(delay: int) -> tuple[Sequence[complex], Sequence[complex]]:
        if delay < 0:
            return first[-delay:], second[:delay]
        if delay > 0:
            return first[:-delay], second[delay:]
        return first, second

    def coherence(
        left: Sequence[complex], right: Sequence[complex], conjugate: bool = False
    ) -> tuple[float, complex, float, float]:
        left_energy = math.fsum(abs(value) ** 2 for value in left)
        right_energy = math.fsum(abs(value) ** 2 for value in right)
        if conjugate:
            numerator = sum(a * b for a, b in zip(left, right, strict=True))
        else:
            numerator = sum(a.conjugate() * b for a, b in zip(left, right, strict=True))
        denominator = math.sqrt(left_energy * right_energy)
        return (
            abs(numerator) / denominator if denominator else 0.0,
            numerator,
            left_energy,
            right_energy,
        )

    candidates: list[tuple[float, int, complex, float, float]] = []
    for delay in range(-max_delay_samples, max_delay_samples + 1):
        left, right = aligned(delay)
        score, numerator, left_energy, right_energy = coherence(left, right)
        candidates.append((score, delay, numerator, left_energy, right_energy))
    # Stable tie-break: prefer the smallest absolute delay, then negative.
    score, delay, numerator, left_energy, right_energy = max(
        candidates, key=lambda item: (item[0], -abs(item[1]), -item[1])
    )
    left, right = aligned(delay)
    gain = numerator / left_energy if left_energy else 0j
    residual_power = math.fsum(
        abs(b - gain * a) ** 2 for a, b in zip(left, right, strict=True)
    )
    residual_fraction = residual_power / right_energy if right_energy else 0.0
    conjugate_score, _, _, _ = coherence(left, right, conjugate=True)
    return PairedEvidence(
        score=score,
        delay_samples=delay,
        relative_phase_rad=math.atan2(gain.imag, gain.real),
        gain_ratio=abs(gain),
        differential_power_fraction=residual_fraction,
        conjugate_score=conjugate_score,
    )


def robust_pair_score(values: Sequence[float]) -> float:
    """Conservative pair aggregation: both receivers must carry evidence."""

    if len(values) != 2:
        raise ValueError("paired detector requires exactly two receiver scores")
    return min(values)
