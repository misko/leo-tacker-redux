"""Exact streaming CI16 sample-quality accumulation."""

from __future__ import annotations

import math
import sys
from array import array
from dataclasses import dataclass


class Ci16DecodeError(ValueError):
    """The recording reader returned bytes inconsistent with CI16 layout."""


def decode_ci16(raw: bytes, receiver_count: int) -> tuple[array[int], int]:
    """Decode complete little-endian ``sample,receiver,(i,q)`` records."""

    if not isinstance(raw, bytes):
        raise Ci16DecodeError("recording reader must return bytes")
    if receiver_count <= 0:
        raise Ci16DecodeError("receiver count must be positive")
    bytes_per_sample = receiver_count * 4
    if len(raw) % bytes_per_sample:
        raise Ci16DecodeError("CI16 byte count is not a complete paired sample")
    values = array("h")
    values.frombytes(raw)
    if sys.byteorder != "little":  # pragma: no cover - CI has little-endian hosts
        values.byteswap()
    return values, len(raw) // bytes_per_sample


@dataclass
class ReceiverQualityAccumulator:
    sample_count: int = 0
    sum_i: int = 0
    sum_q: int = 0
    sum_magnitude_squared: int = 0
    peak_abs_component: int = 0
    clipped_component_count: int = 0
    zero_pair_count: int = 0

    def consume(
        self,
        values: array[int],
        *,
        receiver_index: int,
        receiver_count: int,
        sample_count: int,
        clip_threshold_abs: int,
    ) -> None:
        stride = receiver_count * 2
        offset = receiver_index * 2
        stop = sample_count * stride
        for position in range(offset, stop, stride):
            i_value = int(values[position])
            q_value = int(values[position + 1])
            abs_i = abs(i_value)
            abs_q = abs(q_value)
            self.sum_i += i_value
            self.sum_q += q_value
            self.sum_magnitude_squared += i_value * i_value + q_value * q_value
            self.peak_abs_component = max(self.peak_abs_component, abs_i, abs_q)
            self.clipped_component_count += int(abs_i >= clip_threshold_abs) + int(
                abs_q >= clip_threshold_abs
            )
            self.zero_pair_count += int(i_value == 0 and q_value == 0)
        self.sample_count += sample_count

    def summary(self, *, dc_warning_fraction: float) -> ReceiverQuality:
        if self.sample_count <= 0:
            raise Ci16DecodeError("cannot summarize an empty receiver")
        count = self.sample_count
        mean_i = self.sum_i / count
        mean_q = self.sum_q / count
        mean_power = self.sum_magnitude_squared / count
        rms_magnitude = math.sqrt(mean_power)
        ac_power = max(0.0, mean_power - mean_i * mean_i - mean_q * mean_q)
        dc_magnitude = math.hypot(mean_i, mean_q)
        dc_fraction = dc_magnitude / rms_magnitude if rms_magnitude else 0.0
        flags: list[str] = []
        if self.clipped_component_count:
            flags.append("clipping_detected")
        if ac_power == 0.0:
            flags.append("constant_or_zero_input")
        if rms_magnitude and dc_fraction > dc_warning_fraction:
            flags.append("high_dc_fraction")
        return ReceiverQuality(
            sample_count=count,
            component_count=2 * count,
            mean_i=mean_i,
            mean_q=mean_q,
            mean_power=mean_power,
            ac_power=ac_power,
            rms_magnitude=rms_magnitude,
            peak_abs_component=self.peak_abs_component,
            clipped_component_count=self.clipped_component_count,
            clipping_fraction=self.clipped_component_count / (2 * count),
            zero_pair_count=self.zero_pair_count,
            zero_pair_fraction=self.zero_pair_count / count,
            dc_magnitude=dc_magnitude,
            dc_fraction_of_rms=dc_fraction,
            flags=tuple(flags),
        )


@dataclass(frozen=True)
class ReceiverQuality:
    sample_count: int
    component_count: int
    mean_i: float
    mean_q: float
    mean_power: float
    ac_power: float
    rms_magnitude: float
    peak_abs_component: int
    clipped_component_count: int
    clipping_fraction: float
    zero_pair_count: int
    zero_pair_fraction: float
    dc_magnitude: float
    dc_fraction_of_rms: float
    flags: tuple[str, ...]

    def diagnostics(self) -> tuple[tuple[str, int | float], ...]:
        return (
            ("ac_power_counts_squared", self.ac_power),
            ("clipped_component_count", self.clipped_component_count),
            ("clipping_fraction", self.clipping_fraction),
            ("component_count", self.component_count),
            ("dc_fraction_of_rms", self.dc_fraction_of_rms),
            ("dc_i_counts", self.mean_i),
            ("dc_magnitude_counts", self.dc_magnitude),
            ("dc_q_counts", self.mean_q),
            ("mean_power_counts_squared", self.mean_power),
            ("peak_abs_component_counts", self.peak_abs_component),
            ("rms_magnitude_counts", self.rms_magnitude),
            ("sample_count", self.sample_count),
            ("zero_pair_count", self.zero_pair_count),
            ("zero_pair_fraction", self.zero_pair_fraction),
        )
