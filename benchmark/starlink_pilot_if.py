"""Generate a detector-independent Starlink edge-pilot IF fixture.

The waveform contains a selectable subset of the published 4QAM edge-pilot
codes from Qin et al., arXiv:2602.02627, Appendix A.  It is baseband IQ for an
SDR whose TX local oscillator is set to the center of one edge-pilot band; it
does not generate Ku-band RF or model the complete Starlink downlink.

Generation is standard-library-only and produces a single-channel CI16 stream.
It has no dependency on capture, analysis, dashboard, or detector code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SCHEMA = "leo-flow.starlink-edge-pilot-if-fixture/v1"
GENERATOR_ID = "benchmark.starlink_pilot_if/v1"
PAPER_REFERENCE = "Qin-et-al-arXiv-2602.02627-appendix-a"

FRAME_RATE_HZ = 750
OFDM_SYMBOL_DURATION_S = 4.4e-6
CYCLIC_PREFIX_DURATION_S = (2 / 15) * 1e-6
SUBCARRIER_SPACING_HZ = 234_375.0
EDGE_PILOT_SUBCARRIERS: dict[str, tuple[int, ...]] = {
    "upper": tuple(range(488, 496)),
    "lower": tuple(range(528, 536)),
}

# One 600-bit integer encodes the 300 base-4 coefficients for symbols 2..301.
# These values are transcribed from Qin et al., Appendix A.  The legacy
# repository is a numerical oracle only and is not imported at runtime.
EDGE_PILOT_HEX = {
    488: "7634046DA45F89042D0117E163167D4AE832D857515F3CAD90337697FB8F1CD048EFEF559ECD79688BCBBF44D2FA9BDFAE639DB5D7B1DD2DDCE4EC9733C0D4DCCF3172A0EC34CC226C530E",
    489: "CD9AFAA654147A5FE2B407B51FFE15215B3A71624139619628A9C33E8E3A32E5146C09BD3EE9026CA52032D7FD38960FFC52599E9B8A7F6942334BD4C6D99D4331DEF5674570B245FBB25F",
    490: "02481A2B278B88F096C8D174D369D0CF6781B70EBD402D6A6F4C985DA6265866A8374DC0B3E4917146FE3274CA5D61C3F9A31CB8125F291155CBD4F4F84E93C0D854BBC54EE14443EC2DF8",
    491: "D8DC99C2654265B8C32450114C37E2B725A822F1054B46F272877122E47109F113D59E37DFF418FEA3627C7A5CC0A93ABA0F9408E958DF4179C4DE40CEF842D333632B3E77BEB34B2E6045",
    492: "3CC5CA83B0D33089B14C3B6AC3D1946359726B4966B2E966BE61124A5D53E22A73EDBEB383A92F06CA6CAA8A5B1ECE695465145E286EEE1804CD79A00C84FC80C87DE9DF572F9B54AE798B",
    493: "C77BD59D15C2C917EEC97FB479B9F0B2BF5D2ECCD80248D2AC68C84CEA11BAD18D9F6F31B6AFD783347943562E2C6832EA76828FCDAB31EFF6A9A88EA48E3AFA625B2FCDA7B99B0295E926",
    494: "6152EF153B85110FB0B7E24D8334B1C4196DE872B598767BC3CB4A4827A09D924AA7F57EB946F1981D036E3001934B10C9E22ABB6AF1F047B3A874CA95E68CBA67063F605FD05D532AAD3C",
    495: "CD8CACF9DEFACD2CB9811439D8B7E16F9E09BED47370207150A86DFE24EA1298CCB0907F5BAB67D4660462C6B10F74B8D9FA7B6F9EC1399B30B43AF622A894B2220B6B509A84AABB58D023",
    528: "CCBF3A16929836160CEC6EB7417AE6C37DC1E828CEFB60CE0E6C3B546A76B0AE1E7BC0E9577528B0F78F82A4104EA2C316B945D385200C7E5A1C5B48F5F9F9AF5C4BA920ACA3A599DB9974",
    529: "9CF72F5F5B95CE7342C925CF1AAF457F182C32810E2F7486705D5FA2D9C8923B0173FB206B46045C6F162BB9FFD051DB5E5900EFD2DE24D4BB3FE87DD776F00B5613A7D22B2821E139A599",
    530: "296319D723210189953BB730DC6046E4EC5FB48F9718D5B600A01578CAC3159B58EE8A306663921FBE78EE7C1E8E049B4230A14EB4954933AB64F67B396DD6DB12BCBB3CCA60EA79E0614B",
    531: "1017FBBD3D03981EE9F4424D473B8A73E136C777956EAEBD4CA51E9B70D9F5D10657F268595A5C3687D2DD06C98630F817CABEF3EE660822350A70F10A29A8740212A9CF7E7D814D60A69C",
    532: "712EA482B28E96676E65D09994965587314F2B562D0E750FE566E89205A8D4DFED2C4FAFFC5ED1EA6FB63EC13513444006B78ADFB4BDB6CB05470601C9F8F4901423069C9FBD68D292C16F",
    533: "584E9F48ACA08784E696644C78ED9684FC484F32AA1B4DA8E95457358DF89FE8B9D84D47F30D3CA2F2DDF0E76E57F14A44675326EDCF15052CB62B7DF0EBE623057605CF2406E25BD56B3B",
    534: "4AF2ECF32983A9E781852F6E90DC6CCE901863F527E038DA22C0CE02E44FA0563718D93E7454293962B43594CC2EE427FAE6F15C1238D9C85ABC4E303F3AEC3404A52310CAC0378665E19A",
    535: "084AA73DF9F60535829A716EC94D95AA6901B41E81AEF28B03F08CDE7D45425B1164009D56459C4286E269F4B8EBDBA8BF6FC79847B08A69F79AF6E6A7AF05DA504455BA72727DD7BE7744",
}


class PilotIfSpecificationError(ValueError):
    """The requested fixture is invalid or would be unsafe to quantize."""


@dataclass(frozen=True)
class PilotIfSpecification:
    sample_rate_hz: int
    sample_count: int
    edge: Literal["lower", "upper"]
    pilot_indices: tuple[int, ...]
    signal_rms_counts: float = 128.0
    noise_snr_db: float | None = None
    seed_u64: int = 1
    cfo_hz: float = 0.0
    if_center_hz: float | None = None
    frame_phase: Literal["random", "coherent"] = "random"
    converter_min: int = -2048
    converter_max: int = 2047

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0 or self.sample_count <= 0:
            raise PilotIfSpecificationError("sample rate and count must be positive")
        if self.edge not in EDGE_PILOT_SUBCARRIERS:
            raise PilotIfSpecificationError("edge must be lower or upper")
        if not self.pilot_indices or len(set(self.pilot_indices)) != len(
            self.pilot_indices
        ):
            raise PilotIfSpecificationError("pilot indices must be nonempty and unique")
        allowed = set(EDGE_PILOT_SUBCARRIERS[self.edge])
        if not set(self.pilot_indices) <= allowed:
            raise PilotIfSpecificationError(
                f"pilot indices must belong to the published {self.edge} edge"
            )
        if tuple(sorted(self.pilot_indices)) != self.pilot_indices:
            raise PilotIfSpecificationError("pilot indices must be sorted")
        if not math.isfinite(self.signal_rms_counts) or self.signal_rms_counts <= 0:
            raise PilotIfSpecificationError("signal RMS must be finite and positive")
        if self.noise_snr_db is not None and not math.isfinite(self.noise_snr_db):
            raise PilotIfSpecificationError("noise SNR must be finite")
        if not 0 < self.seed_u64 < 2**64:
            raise PilotIfSpecificationError("seed_u64 must lie in [1, 2**64)")
        if not math.isfinite(self.cfo_hz):
            raise PilotIfSpecificationError("CFO must be finite")
        if self.if_center_hz is not None and (
            not math.isfinite(self.if_center_hz) or self.if_center_hz <= 0
        ):
            raise PilotIfSpecificationError("IF center must be finite and positive")
        if self.frame_phase not in ("random", "coherent"):
            raise PilotIfSpecificationError("frame phase must be random or coherent")
        if self.converter_min < -32768 or self.converter_max > 32767:
            raise PilotIfSpecificationError("converter limits must fit signed int16")
        if self.converter_min >= self.converter_max:
            raise PilotIfSpecificationError("converter limits must be ordered")
        nyquist = self.sample_rate_hz / 2
        outside = [
            offset
            for offset in pilot_local_offsets_hz(self.edge, self.pilot_indices)
            if abs(offset + self.cfo_hz) >= nyquist
        ]
        if outside:
            raise PilotIfSpecificationError(
                "selected pilot centers do not fit inside the requested sample rate"
            )


@dataclass(frozen=True)
class PilotIfWaveform:
    """Immutable bytes and canonical truth metadata for one generated fixture."""

    ci16_le: bytes
    truth_json: bytes

    @property
    def truth(self) -> dict[str, Any]:
        return json.loads(self.truth_json)


def subcarrier_offset_hz(index: int) -> float:
    if index not in range(1024):
        raise PilotIfSpecificationError("subcarrier index must lie in [0, 1024)")
    signed = index if index < 512 else index - 1024
    return signed * SUBCARRIER_SPACING_HZ


def edge_pilot_center_offset_hz(edge: str) -> float:
    try:
        indexes = EDGE_PILOT_SUBCARRIERS[edge]
    except KeyError as exc:
        raise PilotIfSpecificationError("edge must be lower or upper") from exc
    return sum(subcarrier_offset_hz(index) for index in indexes) / len(indexes)


def pilot_local_offsets_hz(
    edge: str, pilot_indices: tuple[int, ...] | None = None
) -> tuple[float, ...]:
    try:
        indexes = (
            EDGE_PILOT_SUBCARRIERS[edge] if pilot_indices is None else pilot_indices
        )
    except KeyError as exc:
        raise PilotIfSpecificationError("edge must be lower or upper") from exc
    center = edge_pilot_center_offset_hz(edge)
    return tuple(subcarrier_offset_hz(index) - center for index in indexes)


def pilot_symbol_state(subcarrier: int, symbol_index: int) -> int:
    """Return the published base-4 state for OFDM symbol 2..301."""

    if subcarrier not in EDGE_PILOT_HEX:
        raise PilotIfSpecificationError("no published code for subcarrier")
    if symbol_index not in range(2, 302):
        raise PilotIfSpecificationError("pilot symbol index must lie in [2, 301]")
    encoded = int(EDGE_PILOT_HEX[subcarrier], 16)
    return (encoded >> (2 * (301 - symbol_index))) & 3


def generate_pilot_if(specification: PilotIfSpecification) -> PilotIfWaveform:
    offsets = pilot_local_offsets_hz(specification.edge, specification.pilot_indices)
    signal = _unscaled_signal(specification, offsets)
    active = tuple(index for index, value in enumerate(signal) if value != 0j)
    if not active:
        raise PilotIfSpecificationError(
            "sample interval contains no coded pilot symbols"
        )
    raw_signal_rms = math.sqrt(
        sum(abs(signal[index]) ** 2 for index in active) / len(active)
    )
    signal_scale = specification.signal_rms_counts / raw_signal_rms
    scaled_signal = [value * signal_scale for value in signal]

    noise = [0j] * specification.sample_count
    requested_noise_rms = 0.0
    achieved_snr_db: float | None = None
    if specification.noise_snr_db is not None:
        requested_noise_rms = specification.signal_rms_counts / math.sqrt(
            10 ** (specification.noise_snr_db / 10)
        )
        noise = _unit_uniform_noise(specification.sample_count, specification.seed_u64)
        raw_noise_rms = math.sqrt(
            sum(abs(noise[index]) ** 2 for index in active) / len(active)
        )
        noise_scale = requested_noise_rms / raw_noise_rms
        noise = [value * noise_scale for value in noise]
        achieved_noise_power = sum(abs(noise[index]) ** 2 for index in active) / len(
            active
        )
        achieved_signal_power = sum(
            abs(scaled_signal[index]) ** 2 for index in active
        ) / len(active)
        achieved_snr_db = 10 * math.log10(achieved_signal_power / achieved_noise_power)

    combined = [a + b for a, b in zip(scaled_signal, noise, strict=True)]
    maximum_unquantized_component = max(
        max(abs(value.real), abs(value.imag)) for value in combined
    )
    if (
        maximum_unquantized_component > specification.converter_max + 0.5
        or -maximum_unquantized_component < specification.converter_min - 0.5
    ):
        raise PilotIfSpecificationError(
            "waveform would clip the configured converter envelope; reduce level"
        )

    output = bytearray()
    peak_component = 0
    sum_quantized_power = 0
    for value in combined:
        i_value = _round_ties_away_from_zero(value.real)
        q_value = _round_ties_away_from_zero(value.imag)
        if not (
            specification.converter_min <= i_value <= specification.converter_max
            and specification.converter_min <= q_value <= specification.converter_max
        ):
            raise PilotIfSpecificationError("quantized waveform would clip")
        output.extend(struct.pack("<hh", i_value, q_value))
        peak_component = max(peak_component, abs(i_value), abs(q_value))
        sum_quantized_power += i_value * i_value + q_value * q_value

    ci16 = bytes(output)
    specification_doc = _specification_document(specification, offsets)
    specification_json = _canonical_json(specification_doc)
    truth = {
        "schema": SCHEMA,
        "generator": GENERATOR_ID,
        "paper_reference": PAPER_REFERENCE,
        "model_scope": (
            "published coded edge pilots only; not the complete Starlink downlink"
        ),
        "domain": "complex baseband at an SDR tuned to the edge-pilot IF center",
        "specification": specification_doc,
        "specification_sha256": hashlib.sha256(specification_json).hexdigest(),
        "sample_contract": {
            "dtype": "int16_le",
            "layout": "sample,component",
            "component_order": ["i", "q"],
            "bytes_per_sample": 4,
        },
        "frame_truth": {
            "frame_rate_hz": FRAME_RATE_HZ,
            "ofdm_symbol_duration_s": OFDM_SYMBOL_DURATION_S,
            "cyclic_prefix_duration_s": CYCLIC_PREFIX_DURATION_S,
            "coded_symbol_indices": [2, 301],
            "active_sample_count": len(active),
        },
        "level_truth": {
            "requested_signal_rms_counts": specification.signal_rms_counts,
            "requested_noise_snr_db": specification.noise_snr_db,
            "achieved_prequantization_snr_db": achieved_snr_db,
            "requested_noise_rms_counts": requested_noise_rms,
            "quantized_combined_rms_counts": math.sqrt(
                sum_quantized_power / specification.sample_count
            ),
            "peak_component_counts": peak_component,
            "clipped_component_count": 0,
            "snr_basis": "signal/noise power over coded-pilot samples before quantization",
        },
        "bytes": len(ci16),
        "ci16_sha256": hashlib.sha256(ci16).hexdigest(),
    }
    return PilotIfWaveform(ci16_le=ci16, truth_json=_canonical_json(truth))


def _unscaled_signal(
    specification: PilotIfSpecification, offsets: tuple[float, ...]
) -> list[complex]:
    output: list[complex] = []
    pilot_count_scale = math.sqrt(len(specification.pilot_indices))
    qpsk = tuple(
        complex(
            math.cos(math.pi / 2 * (state + 0.5)), math.sin(math.pi / 2 * (state + 0.5))
        )
        for state in range(4)
    )
    for sample_index in range(specification.sample_count):
        time_s = sample_index / specification.sample_rate_hz
        frame_index = (sample_index * FRAME_RATE_HZ) // specification.sample_rate_hz
        frame_time_s = frame_index / FRAME_RATE_HZ
        local_time_s = time_s - frame_time_s
        symbol_index = int(local_time_s / OFDM_SYMBOL_DURATION_S)
        if symbol_index not in range(2, 302):
            output.append(0j)
            continue
        phase = _frame_phase(specification, frame_index)
        value = 0j
        symbol_time_s = local_time_s - symbol_index * OFDM_SYMBOL_DURATION_S
        for subcarrier, offset_hz in zip(
            specification.pilot_indices, offsets, strict=True
        ):
            code = qpsk[pilot_symbol_state(subcarrier, symbol_index)]
            angle = 2 * math.pi * offset_hz * (symbol_time_s - CYCLIC_PREFIX_DURATION_S)
            value += code * complex(math.cos(angle), math.sin(angle))
        cfo_angle = 2 * math.pi * specification.cfo_hz * time_s
        carrier = complex(math.cos(cfo_angle + phase), math.sin(cfo_angle + phase))
        output.append(value * carrier / pilot_count_scale)
    return output


def _frame_phase(specification: PilotIfSpecification, frame_index: int) -> float:
    if specification.frame_phase == "coherent":
        return 0.0
    digest = hashlib.sha256(
        f"{specification.seed_u64}:frame:{frame_index}".encode("ascii")
    ).digest()
    phase_q64 = int.from_bytes(digest[:8], "big")
    return 2 * math.pi * phase_q64 / 2**64


def _unit_uniform_noise(count: int, seed: int) -> list[complex]:
    state = seed
    result: list[complex] = []
    for _ in range(count):
        state, first = _xorshift64star(state)
        state, second = _xorshift64star(state)
        real = 2 * ((first >> 11) / 2**53) - 1
        imag = 2 * ((second >> 11) / 2**53) - 1
        result.append(complex(real, imag))
    return result


def _xorshift64star(state: int) -> tuple[int, int]:
    mask64 = (1 << 64) - 1
    state ^= state >> 12
    state ^= (state << 25) & mask64
    state ^= state >> 27
    state &= mask64
    return state, (state * 2685821657736338717) & mask64


def _round_ties_away_from_zero(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def _specification_document(
    specification: PilotIfSpecification, offsets: tuple[float, ...]
) -> dict[str, Any]:
    return {
        "sample_rate_hz": specification.sample_rate_hz,
        "sample_count": specification.sample_count,
        "edge": specification.edge,
        "pilot_indices": list(specification.pilot_indices),
        "pilot_local_offsets_hz": list(offsets),
        "subcarrier_spacing_hz": SUBCARRIER_SPACING_HZ,
        "edge_center_offset_from_channel_hz": edge_pilot_center_offset_hz(
            specification.edge
        ),
        "signal_rms_counts": specification.signal_rms_counts,
        "noise_snr_db": specification.noise_snr_db,
        "noise_distribution": (
            None
            if specification.noise_snr_db is None
            else "deterministic independent uniform components, RMS-normalized"
        ),
        "seed_u64": specification.seed_u64,
        "cfo_hz": specification.cfo_hz,
        "if_center_hz": specification.if_center_hz,
        "frame_phase": specification.frame_phase,
        "converter_min": specification.converter_min,
        "converter_max": specification.converter_max,
    }


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-rate", type=int, default=5_000_000)
    parser.add_argument("--sample-count", type=int)
    parser.add_argument("--edge", choices=("lower", "upper"), default="lower")
    parser.add_argument("--pilots", default="531,532")
    parser.add_argument("--signal-rms", type=float, default=128.0)
    parser.add_argument("--noise-snr-db", type=float)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--cfo-hz", type=float, default=0.0)
    parser.add_argument("--if-center-hz", type=float)
    parser.add_argument(
        "--frame-phase", choices=("random", "coherent"), default="random"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    sample_count = args.sample_count or round(3 * args.sample_rate / FRAME_RATE_HZ)
    try:
        pilots = tuple(sorted(int(item) for item in args.pilots.split(",")))
        waveform = generate_pilot_if(
            PilotIfSpecification(
                sample_rate_hz=args.sample_rate,
                sample_count=sample_count,
                edge=args.edge,
                pilot_indices=pilots,
                signal_rms_counts=args.signal_rms,
                noise_snr_db=args.noise_snr_db,
                seed_u64=args.seed,
                cfo_hz=args.cfo_hz,
                if_center_hz=args.if_center_hz,
                frame_phase=args.frame_phase,
            )
        )
        args.output.write_bytes(waveform.ci16_le)
        truth_path = args.output.with_suffix(args.output.suffix + ".truth.json")
        truth_path.write_bytes(waveform.truth_json)
    except (OSError, PilotIfSpecificationError, ValueError) as exc:
        print(f"INVALID: {exc}")
        return 2
    truth = waveform.truth
    print(
        f"VALID bytes={truth['bytes']} sha256={truth['ci16_sha256']} truth={truth_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
