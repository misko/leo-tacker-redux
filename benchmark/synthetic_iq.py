#!/usr/bin/env python3
"""Detector-independent, integer-exact synthetic CI16 fixture generator.

The generator is intentionally small and standard-library-only.  Its QPSK
numerically controlled oscillator is not intended to model Starlink.  It
exists to test frequency/drift bookkeeping, paired-receiver layout, delay,
gain, clipping, quantization and deterministic replay without sharing code
with any detector.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmark.validate import ValidationError, load_json, validate_synthetic_spec


MASK64 = (1 << 64) - 1
Q20 = 1 << 20
QPSK = ((1, 0), (0, 1), (-1, 0), (0, -1))


def _xorshift64star(state: int) -> tuple[int, int]:
    state ^= state >> 12
    state ^= (state << 25) & MASK64
    state ^= state >> 27
    state &= MASK64
    return state, (state * 2685821657736338717) & MASK64


def _noise(state: int, peak: int) -> tuple[int, int]:
    state, value = _xorshift64star(state)
    return state, value % (2 * peak + 1) - peak


def _round_q20(value: int, gain_q20: int) -> int:
    numerator = value * gain_q20
    if numerator >= 0:
        return (numerator + Q20 // 2) // Q20
    return -((-numerator + Q20 // 2) // Q20)


def _signal_component(case: Mapping[str, Any], source_index: int,
                      receiver: Mapping[str, Any]) -> tuple[int, int]:
    if source_index < 0:
        return 0, 0
    signal = case["signal_truth"]
    phase = (
        int(signal["phase_start_q64"])
        + source_index * int(signal["phase_step_start_q64"])
        + source_index * (source_index - 1) // 2 * int(signal["phase_step_delta_q64"])
        + int(receiver["phase_offset_q64"])
    ) & MASK64
    i_unit, q_unit = QPSK[phase >> 62]
    amplitude = int(signal["amplitude_counts"])
    return amplitude * i_unit, amplitude * q_unit


def generate_case(case: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """Generate one exact ``sample,receiver,component`` CI16 byte stream."""

    state = int(case["seed_u64"])
    if state == 0:
        raise ValidationError("xorshift64star seed must be nonzero")
    noise_peak = int(case["noise_truth"]["uniform_component_peak_counts"])
    clip_min = int(case["quantization_truth"]["clip_min"])
    clip_max = int(case["quantization_truth"]["clip_max"])
    output = bytearray()
    clipped_by_receiver = [0, 0]

    for sample_index in range(int(case["sample_count"])):
        for receiver_index, receiver in enumerate(case["receiver_truth"]):
            delay = int(receiver["delay_samples"])
            signal_i, signal_q = _signal_component(case, sample_index - delay, receiver)
            state, noise_i = _noise(state, noise_peak)
            state, noise_q = _noise(state, noise_peak)
            gain_q20 = int(receiver["gain_q20"])
            values = (
                _round_q20(signal_i + noise_i, gain_q20) + int(receiver["dc_i_counts"]),
                _round_q20(signal_q + noise_q, gain_q20) + int(receiver["dc_q_counts"]),
            )
            clipped: list[int] = []
            for value in values:
                bounded = min(clip_max, max(clip_min, value))
                if bounded != value:
                    clipped_by_receiver[receiver_index] += 1
                clipped.append(bounded)
            output.extend(struct.pack("<hh", clipped[0], clipped[1]))

    result = bytes(output)
    truth = {
        "bytes": len(result),
        "ci16_sha256": hashlib.sha256(result).hexdigest(),
        "clipped_component_count": sum(clipped_by_receiver),
        "clipped_component_count_by_receiver": clipped_by_receiver,
        "final_prng_state_u64": state,
    }
    return result, truth


def verify_spec(spec: Mapping[str, Any]) -> None:
    validate_synthetic_spec(spec)
    for case in spec["cases"]:
        _, observed = generate_case(case)
        expected = case["expected_truth"]
        for key in (
            "bytes", "ci16_sha256", "clipped_component_count",
            "clipped_component_count_by_receiver", "final_prng_state_u64",
        ):
            if observed[key] != expected[key]:
                raise ValidationError(
                    f"{case['case_id']}: {key} {observed[key]!r} != {expected[key]!r}"
                )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--case", dest="case_id")
    parser.add_argument("--output", type=Path,
                        help="explicitly materialize one small fixture")
    args = parser.parse_args(argv)
    try:
        spec = load_json(args.spec)
        verify_spec(spec)
        if args.output:
            if not args.case_id:
                raise ValidationError("--output requires --case")
            matches = [case for case in spec["cases"] if case["case_id"] == args.case_id]
            if len(matches) != 1:
                raise ValidationError(f"unknown --case {args.case_id}")
            data, _ = generate_case(matches[0])
            args.output.write_bytes(data)
    except (OSError, ValidationError) as exc:
        print(f"INVALID: {exc}")
        return 2
    print(f"VALID synthetic cases={len(spec['cases'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
