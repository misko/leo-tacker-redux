from __future__ import annotations

import hashlib
import json
import math
import struct

import pytest

from benchmark.starlink_pilot_if import (
    EDGE_PILOT_HEX,
    EDGE_PILOT_SUBCARRIERS,
    PilotIfSpecification,
    PilotIfSpecificationError,
    edge_pilot_center_offset_hz,
    generate_pilot_if,
    main,
    pilot_local_offsets_hz,
    pilot_symbol_state,
)


def test_geometry_matches_published_and_legacy_oracle_values() -> None:
    assert EDGE_PILOT_SUBCARRIERS == {
        "upper": tuple(range(488, 496)),
        "lower": tuple(range(528, 536)),
    }
    expected_offsets = (
        -820_312.5,
        -585_937.5,
        -351_562.5,
        -117_187.5,
        117_187.5,
        351_562.5,
        585_937.5,
        820_312.5,
    )
    assert pilot_local_offsets_hz("lower") == expected_offsets
    assert pilot_local_offsets_hz("upper") == expected_offsets
    assert edge_pilot_center_offset_hz("lower") == -115_429_687.5
    assert edge_pilot_center_offset_hz("upper") == 115_195_312.5


def test_published_code_prefixes_are_fixed_golden_values() -> None:
    code_payload = "\n".join(
        f"{index}:{EDGE_PILOT_HEX[index]}" for index in sorted(EDGE_PILOT_HEX)
    ).encode()
    assert hashlib.sha256(code_payload).hexdigest() == (
        "a953523af4d7126d8b619ab3dbbc94469ad390cce224d64bd7f26e8f55db397c"
    )
    assert [pilot_symbol_state(488, index) for index in range(2, 10)] == [
        1,
        3,
        1,
        2,
        0,
        3,
        1,
        0,
    ]
    assert [pilot_symbol_state(531, index) for index in range(2, 10)] == [
        0,
        1,
        0,
        0,
        0,
        1,
        1,
        3,
    ]
    assert [pilot_symbol_state(532, index) for index in range(2, 10)] == [
        1,
        3,
        0,
        1,
        0,
        2,
        3,
        2,
    ]


def test_full_pilot_band_rejects_1_25_msps_but_inner_pair_fits() -> None:
    with pytest.raises(PilotIfSpecificationError, match="do not fit"):
        PilotIfSpecification(
            sample_rate_hz=1_250_000,
            sample_count=5_000,
            edge="lower",
            pilot_indices=tuple(range(528, 536)),
        )

    specification = PilotIfSpecification(
        sample_rate_hz=1_250_000,
        sample_count=5_000,
        edge="lower",
        pilot_indices=(531, 532),
    )
    assert pilot_local_offsets_hz(specification.edge, specification.pilot_indices) == (
        -117_187.5,
        117_187.5,
    )


def test_single_pilot_samples_follow_independently_computed_symbol_math() -> None:
    specification = PilotIfSpecification(
        sample_rate_hz=5_000_000,
        sample_count=20_000,
        edge="lower",
        pilot_indices=(531,),
        signal_rms_counts=500.0,
        frame_phase="coherent",
    )
    waveform = generate_pilot_if(specification)

    # Symbol 2 starts at sample 44 at 5 MS/s.  Independently reconstruct the
    # published state-0 4QAM symbol and the -117187.5 Hz local pilot carrier.
    sample_index = 44
    symbol_start_s = 2 * 4.4e-6
    time_s = sample_index / specification.sample_rate_hz
    symbol_time_s = time_s - symbol_start_s
    qpsk = complex(1 / math.sqrt(2), 1 / math.sqrt(2))
    angle = 2 * math.pi * -117_187.5 * (symbol_time_s - (2 / 15) * 1e-6)
    expected = 500 * qpsk * complex(math.cos(angle), math.sin(angle))
    observed = complex(*struct.unpack_from("<hh", waveform.ci16_le, sample_index * 4))
    assert observed.real == pytest.approx(expected.real, abs=0.51)
    assert observed.imag == pytest.approx(expected.imag, abs=0.51)


def test_seeded_noise_is_deterministic_and_snr_truth_is_measured() -> None:
    specification = PilotIfSpecification(
        sample_rate_hz=2_500_000,
        sample_count=10_000,
        edge="lower",
        pilot_indices=tuple(range(528, 536)),
        signal_rms_counts=128.0,
        noise_snr_db=-12.0,
        seed_u64=0x123456789ABCDEF,
    )
    first = generate_pilot_if(specification)
    second = generate_pilot_if(specification)
    changed = generate_pilot_if(
        PilotIfSpecification(
            **{
                **specification.__dict__,
                "seed_u64": specification.seed_u64 + 1,
            }
        )
    )

    assert first == second
    assert first.ci16_le != changed.ci16_le
    assert len(first.ci16_le) == specification.sample_count * 4
    assert first.truth["level_truth"]["achieved_prequantization_snr_db"] == (
        pytest.approx(-12.0, abs=1e-12)
    )
    assert first.truth["level_truth"]["clipped_component_count"] == 0
    assert json.loads(first.truth_json)["ci16_sha256"] == first.truth["ci16_sha256"]


def test_generation_fails_closed_instead_of_clipping() -> None:
    specification = PilotIfSpecification(
        sample_rate_hz=2_500_000,
        sample_count=10_000,
        edge="lower",
        pilot_indices=tuple(range(528, 536)),
        signal_rms_counts=2_000.0,
        frame_phase="coherent",
    )
    with pytest.raises(PilotIfSpecificationError, match="clip"):
        generate_pilot_if(specification)


def test_cli_materializes_only_explicit_waveform_and_truth(tmp_path) -> None:
    output = tmp_path / "two-pilot.ci16"
    assert (
        main(
            (
                "--sample-rate",
                "2500000",
                "--sample-count",
                "10000",
                "--edge",
                "lower",
                "--pilots",
                "531,532",
                "--output",
                str(output),
            )
        )
        == 0
    )
    assert output.stat().st_size == 40_000
    truth = json.loads(output.with_suffix(".ci16.truth.json").read_bytes())
    assert truth["specification"]["pilot_indices"] == [531, 532]
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "two-pilot.ci16",
        "two-pilot.ci16.truth.json",
    ]
