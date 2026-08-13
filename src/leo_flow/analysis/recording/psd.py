"""Small deterministic radix-2 PSD summary for bounded CI16 windows."""

from __future__ import annotations

import cmath
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class CompactPsd:
    peak_bin: int
    frequency_offset_hz: float
    peak_power: float
    median_noise_power: float
    peak_to_median_ratio: float
    snr_db: float


def radix2_fft(values: Sequence[complex]) -> list[complex]:
    """Return an unnormalized, deterministic radix-2 DFT."""

    size = len(values)
    if size < 2 or size & (size - 1):
        raise ValueError("FFT window length must be a power of two >= 2")
    result = [complex(value) for value in values]
    target = 0
    for source in range(1, size):
        bit = size >> 1
        while target & bit:
            target ^= bit
            bit >>= 1
        target ^= bit
        if source < target:
            result[source], result[target] = result[target], result[source]
    length = 2
    while length <= size:
        root = cmath.exp(-2j * math.pi / length)
        half = length // 2
        for start in range(0, size, length):
            twiddle = 1.0 + 0.0j
            for offset in range(half):
                even = result[start + offset]
                odd = twiddle * result[start + offset + half]
                result[start + offset] = even + odd
                result[start + offset + half] = even - odd
                twiddle *= root
        length *= 2
    return result


def compact_psd(
    samples: Sequence[complex],
    *,
    sample_rate_hz: float,
    noise_floor_epsilon: float,
) -> CompactPsd:
    """Summarize a rectangular-window PSD after removing complex DC."""

    size = len(samples)
    if size < 2:
        raise ValueError("PSD requires at least two samples")
    mean = sum(samples) / size
    spectrum = radix2_fft([sample - mean for sample in samples])
    scale = float(size * size)
    powers = [
        (value.real * value.real + value.imag * value.imag) / scale
        for value in spectrum
    ]
    peak_bin = max(range(size), key=lambda index: (powers[index], -index))
    excluded = {0, peak_bin, (peak_bin - 1) % size, (peak_bin + 1) % size}
    background = [power for index, power in enumerate(powers) if index not in excluded]
    median_noise = statistics.median(background) if background else 0.0
    denominator = max(median_noise, noise_floor_epsilon)
    ratio = powers[peak_bin] / denominator
    snr_db = 10.0 * math.log10(max(ratio, noise_floor_epsilon))
    signed_bin = peak_bin if peak_bin < size // 2 else peak_bin - size
    return CompactPsd(
        peak_bin=peak_bin,
        frequency_offset_hz=signed_bin * sample_rate_hz / size,
        peak_power=powers[peak_bin],
        median_noise_power=median_noise,
        peak_to_median_ratio=ratio,
        snr_db=snr_db,
    )
