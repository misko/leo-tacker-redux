from __future__ import annotations

import hashlib
import json
import math
import struct
from pathlib import Path

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
from benchmark.starlink_scan_fixture import (
    FrozenPairedBackground,
    ReceiverPath,
    StarlinkPilotScanCase,
    StarlinkScanFixtureError,
    generate_paired_starlink_scan_fixture,
)
from leo_flow.capture.scan_plan import (
    StarlinkEdgeScanSpec,
    build_starlink_edge_scan_plan,
)
from leo_flow.contracts.capture import GainMode, GainSetting
from leo_flow.contracts.core import PlanId, RadioId, ReceiverChainId


def _scan_plan():
    return build_starlink_edge_scan_plan(
        StarlinkEdgeScanSpec(
            plan_id=PlanId("plan_recorded_background_fixture"),
            radio_id=RadioId("radio_fixture"),
            receiver_chain_ids=(
                ReceiverChainId("rx_fixture_1"),
                ReceiverChainId("rx_fixture_2"),
            ),
            gain=GainSetting(GainMode.AGC),
            sample_rate_hz=2_500_000.0,
            bandwidth_hz=2_000_000.0,
            sample_count=5_000,
            edge_order="L",
            edge_order_draw_u32=0,
        )
    )


def _recorded_backgrounds(plan, *, value: int = 23):
    requests = tuple(
        segment for activity in plan.activities for segment in activity.segments
    )
    result = []
    for index, segment in enumerate(requests):
        sample = struct.pack("<hhhh", value + index, -value, -value - index, value)
        payload = sample * int(segment.sample_count or 0)
        result.append(
            FrozenPairedBackground(
                segment_id=segment.segment_id,
                paired_ci16_le=payload,
                source_recording_id="rec_frozen_real_noise_01",
                declared_source_recording_data_sha256="1" * 64,
                source_start_sample=index * int(segment.sample_count or 0),
                source_segment_sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
    return tuple(result)


def _scan_case(backgrounds, **changes):
    values = {
        "signal_present": True,
        "target_channels": (2,),
        "edge": "lower",
        "pilot_indices": tuple(range(528, 536)),
        "seed_u64": 12345,
        "receiver_paths": (
            ReceiverPath(ambient_noise_rms_counts=0.0),
            ReceiverPath(
                integer_delay_samples=3,
                gain_linear=0.7,
                phase_offset_rad=0.25,
                ambient_noise_rms_counts=0.0,
            ),
        ),
        "source_signal_rms_counts": 64.0,
        "recorded_backgrounds": backgrounds,
    }
    values.update(changes)
    return StarlinkPilotScanCase(**values)


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


def test_frequency_drift_is_deterministic_and_bound_in_truth() -> None:
    baseline = PilotIfSpecification(
        sample_rate_hz=2_500_000,
        sample_count=10_000,
        edge="lower",
        pilot_indices=(531, 532),
        signal_rms_counts=64.0,
        cfo_hz=-20_000.0,
        frequency_drift_hz_s=125_000.0,
        frame_phase="coherent",
    )
    first = generate_pilot_if(baseline)
    second = generate_pilot_if(baseline)
    no_drift = generate_pilot_if(
        PilotIfSpecification(**{**baseline.__dict__, "frequency_drift_hz_s": 0.0})
    )

    assert first == second
    assert first.ci16_le != no_drift.ci16_le
    assert first.truth["schema"] == "leo-flow.starlink-edge-pilot-if-fixture/v2"
    assert first.truth["generator"] == "benchmark.starlink_pilot_if/v2"
    assert first.truth["specification"]["frequency_drift_hz_s"] == 125_000.0
    assert no_drift.truth["schema"] == "leo-flow.starlink-edge-pilot-if-fixture/v1"
    assert "frequency_drift_hz_s" not in no_drift.truth["specification"]


def test_frequency_drift_endpoint_must_remain_inside_nyquist() -> None:
    with pytest.raises(PilotIfSpecificationError, match="do not fit"):
        PilotIfSpecification(
            sample_rate_hz=1_250_000,
            sample_count=1_250_000,
            edge="lower",
            pilot_indices=(531, 532),
            frequency_drift_hz_s=600_000.0,
        )


def test_new_drift_fields_preserve_prior_positional_construction() -> None:
    pilot = PilotIfSpecification(
        2_500_000,
        10_000,
        "lower",
        (531, 532),
        128.0,
        None,
        1,
        0.0,
        1_709_687_500.0,
        "random",
        -2048,
        2047,
    )
    scan = StarlinkPilotScanCase(
        True,
        (2,),
        "lower",
        (531, 532),
        1,
        (ReceiverPath(), ReceiverPath()),
        128.0,
        0.0,
        "random",
        -2048,
        2047,
    )

    assert pilot.if_center_hz == 1_709_687_500.0
    assert pilot.frequency_drift_hz_s == 0.0
    assert scan.frame_phase == "random"
    assert scan.frequency_drift_hz_s == 0.0


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


def test_recorded_background_null_is_exact_and_injection_binds_lineage() -> None:
    plan = _scan_plan()
    backgrounds = _recorded_backgrounds(plan)
    null = generate_paired_starlink_scan_fixture(
        plan,
        _scan_case(backgrounds, signal_present=False),
    )
    positive = generate_paired_starlink_scan_fixture(
        plan,
        _scan_case(backgrounds, frequency_drift_hz_s=25_000.0),
    )

    assert tuple(item.paired_ci16_le for item in null.segments) == tuple(
        item.paired_ci16_le for item in backgrounds
    )
    assert null.truth["case"]["background_kind"] == "recorded_receiver_background"
    assert null.truth["schema"] == "leo-flow.paired-starlink-scan-fixture/v2"
    target = next(
        segment
        for segment in positive.truth["segments"]
        if segment["expected_signal_present"]
    )
    assert (
        target["expected_pilot_ending_offsets_hz"]
        != target["expected_pilot_local_offsets_hz"]
    )
    assert target["receivers"][1]["integer_delay_samples"] == 3
    assert target["receivers"][1]["gain_linear"] == 0.7
    for receiver in target["receivers"]:
        lineage = receiver["recorded_background_lineage"]
        assert lineage["schema"] == "leo-flow.recorded-paired-background/v1"
        assert lineage["declared_source_recording_data_sha256"] == "1" * 64
        assert lineage["source_signal_status"] == "unknown"
        assert "not calibrated RF SNR" in receiver["snr_basis"]


def test_recorded_background_clipping_is_rejected_or_saturated_and_reported() -> None:
    plan = _scan_plan()
    backgrounds = _recorded_backgrounds(plan, value=2_020)
    with pytest.raises(StarlinkScanFixtureError, match="would clip"):
        generate_paired_starlink_scan_fixture(plan, _scan_case(backgrounds))

    fixture = generate_paired_starlink_scan_fixture(
        plan,
        _scan_case(backgrounds, clipping_policy="saturate_and_report"),
    )
    target = next(
        segment
        for segment in fixture.truth["segments"]
        if segment["expected_signal_present"]
    )
    assert (
        sum(
            receiver["injection_added_clipped_component_count"]
            for receiver in target["receivers"]
        )
        > 0
    )


def test_injection_null_rejects_preexisting_converter_envelope_violation() -> None:
    plan = _scan_plan()
    backgrounds = _recorded_backgrounds(plan, value=3_000)
    with pytest.raises(StarlinkScanFixtureError, match="would clip"):
        generate_paired_starlink_scan_fixture(
            plan,
            _scan_case(backgrounds, signal_present=False, clipping_policy="reject"),
        )


def test_recorded_background_lineage_and_full_plan_coverage_fail_closed() -> None:
    plan = _scan_plan()
    backgrounds = _recorded_backgrounds(plan)
    with pytest.raises(StarlinkScanFixtureError, match="segment digest"):
        FrozenPairedBackground(
            **{**backgrounds[0].__dict__, "source_segment_sha256": "0" * 64}
        )
    with pytest.raises(StarlinkScanFixtureError, match="cover every scan segment"):
        generate_paired_starlink_scan_fixture(
            plan,
            _scan_case(backgrounds[:-1]),
        )


def test_campaign_spec_spans_required_offline_dimensions_and_forbids_tx() -> None:
    path = (
        Path(__file__).parents[2]
        / "benchmark"
        / "specs"
        / "starlink-fixture-campaign-v1.json"
    )
    campaign = json.loads(path.read_text(encoding="utf-8"))
    conditions = campaign["conditions"]

    assert campaign["schema"] == "leo-flow.starlink-fixture-campaign-spec/v1"
    assert campaign["tx_eligible"] is False
    assert {item["signal_present"] for item in conditions} == {False, True}
    assert (
        len(
            {
                item["injection_to_background_db"]
                for item in conditions
                if item["signal_present"]
            }
        )
        >= 5
    )
    assert any(item["cfo_hz"] != 0 for item in conditions)
    assert {item["frequency_drift_hz_s"] for item in conditions} >= {
        -50_000,
        0,
        50_000,
    }
    assert {item["clipping_policy"] for item in conditions} == {
        "reject",
        "saturate_and_report",
    }
    assert len(campaign["receiver_path_arms"]) >= 3
    assert campaign["background_contract"]["source_signal_status"] == "unknown"
    assert (
        "declared_source_recording_data_sha256"
        in campaign["background_contract"]["required_lineage"]
    )
