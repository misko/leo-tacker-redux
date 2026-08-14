from __future__ import annotations

import hashlib
import math

import pytest

from benchmark.starlink_scan_fixture import (
    ReceiverPath,
    StarlinkPilotScanCase,
    StarlinkScanFixtureError,
    generate_paired_starlink_scan_fixture,
)
from leo_flow.capture.scan_plan import (
    StarlinkEdgeScanSpec,
    build_starlink_edge_scan_plan,
)
from leo_flow.contracts import canonical_digest
from leo_flow.contracts.capture import GainMode, GainSetting
from leo_flow.contracts.core import PlanId, RadioId, ReceiverChainId


def _plan(*, sample_count: int = 5_000, plan_id: str = "plan_paired_starlink_fixture"):
    return build_starlink_edge_scan_plan(
        StarlinkEdgeScanSpec(
            plan_id=PlanId(plan_id),
            radio_id=RadioId("radio_fixture"),
            receiver_chain_ids=(
                ReceiverChainId("rx_fixture_1"),
                ReceiverChainId("rx_fixture_2"),
            ),
            gain=GainSetting(GainMode.AGC),
            sample_rate_hz=2_500_000.0,
            bandwidth_hz=2_000_000.0,
            sample_count=sample_count,
            edge_order="L",
            edge_order_draw_u32=0,
        )
    )


def _case(**changes):
    values = {
        "signal_present": True,
        "target_channels": (2,),
        "edge": "lower",
        "pilot_indices": tuple(range(528, 536)),
        "seed_u64": 0x123456789ABCDEF,
        "source_signal_rms_counts": 64.0,
        "receiver_paths": (
            ReceiverPath(ambient_noise_rms_counts=128.0),
            ReceiverPath(
                integer_delay_samples=7,
                gain_linear=0.5,
                phase_offset_rad=0.3,
                ambient_noise_rms_counts=128.0,
            ),
        ),
    }
    values.update(changes)
    return StarlinkPilotScanCase(**values)


def test_fixture_is_exact_deterministic_paired_ci16_with_bound_truth() -> None:
    plan = _plan()
    first = generate_paired_starlink_scan_fixture(plan, _case())
    second = generate_paired_starlink_scan_fixture(plan, _case())

    assert first == second
    assert first.truth_sha256 == hashlib.sha256(first.truth_json).hexdigest()
    assert first.truth["plan_digest"] == str(canonical_digest(plan))
    assert len(first.segments) == 8
    assert all(
        len(segment.paired_ci16_le) == 5_000 * 2 * 2 * 2 for segment in first.segments
    )
    assert first.truth["sample_contract"] == {
        "dtype": "int16_le",
        "layout": "sample,receiver,component",
        "receiver_chain_ids": ["rx_fixture_1", "rx_fixture_2"],
        "component_order": ["i", "q"],
        "bytes_per_paired_sample": 8,
    }
    assert first.truth["expected_target_segment_ids"] == [
        "seg_plan_paired_starlink_fixture_02_ch2_lower"
    ]
    target = next(
        segment
        for segment in first.truth["segments"]
        if segment["expected_signal_present"]
    )
    assert (
        target["paired_ci16_sha256"]
        == hashlib.sha256(first.payload_for(first.segments[2].segment_id)).hexdigest()
    )
    assert (
        target["receivers"][0]["ci16_sha256"] != target["receivers"][1]["ci16_sha256"]
    )
    assert target["receivers"][0]["integer_delay_samples"] == 0
    assert target["receivers"][1]["integer_delay_samples"] == 7
    assert target["receivers"][1]["gain_linear"] == 0.5
    assert target["receivers"][1]["phase_offset_rad"] == 0.3
    assert target["receivers"][0]["requested_snr_db"] == pytest.approx(
        20 * math.log10(64 / 128)
    )
    assert target["receivers"][0]["achieved_prequantization_snr_db"] == pytest.approx(
        -6.0, abs=0.2
    )
    assert target["receivers"][1]["achieved_prequantization_snr_db"] == pytest.approx(
        -12.0, abs=0.2
    )
    assert target["receivers"][0]["clipped_component_count"] == 0
    assert target["receivers"][0]["peak_component_counts"] > 0
    assert (
        target["expected_pilot_local_offsets_hz"]
        == first.truth["case"]["expected_pilot_offsets_with_cfo_hz"]
    )
    assert target["expected_pilot_center_frequencies_hz"][0] == pytest.approx(
        target["center_frequency_hz"] + target["expected_pilot_local_offsets_hz"][0]
    )
    assert (
        first.truth["source_fixture_truth"]["specification"]["if_center_hz"]
        == (target["center_frequency_hz"])
    )


@pytest.mark.parametrize(
    ("edge", "pilots", "snr", "seed"),
    (
        ("lower", (531, 532), -12.0, 11),
        ("lower", tuple(range(528, 536)), 0.0, 12),
        ("upper", (491, 492), 6.0, 13),
        ("upper", tuple(range(488, 496)), -3.0, 14),
    ),
)
def test_fixture_matrix_covers_edges_pilot_subsets_snr_and_seeds(
    edge: str, pilots: tuple[int, ...], snr: float, seed: int
) -> None:
    case = _case(
        edge=edge,
        pilot_indices=pilots,
        seed_u64=seed,
        source_signal_rms_counts=128.0 * 10 ** (snr / 20),
        receiver_paths=(
            ReceiverPath(ambient_noise_rms_counts=128.0),
            ReceiverPath(integer_delay_samples=3, ambient_noise_rms_counts=128.0),
        ),
    )
    fixture = generate_paired_starlink_scan_fixture(_plan(), case)
    truth = fixture.truth

    assert truth["case"]["edge"] == edge
    assert truth["case"]["pilot_indices"] == list(pilots)
    assert truth["case"]["seed_u64"] == seed
    assert truth["case"]["pilot_local_offsets_hz"]
    target = next(
        segment for segment in truth["segments"] if segment["expected_signal_present"]
    )
    for receiver in target["receivers"]:
        assert receiver["requested_snr_db"] == pytest.approx(snr, abs=1e-12)
        assert receiver["achieved_prequantization_snr_db"] == pytest.approx(
            snr, abs=0.2
        )
        assert receiver["active_signal_sample_count"] > 0
    assert truth["source_fixture_truth"]["ci16_sha256"]


def test_signal_absent_case_has_no_expected_targets_and_independent_noise() -> None:
    case = _case(
        signal_present=False,
        receiver_paths=(
            ReceiverPath(ambient_noise_rms_counts=12.0),
            ReceiverPath(ambient_noise_rms_counts=12.0),
        ),
    )
    fixture = generate_paired_starlink_scan_fixture(_plan(), case)
    truth = fixture.truth

    assert truth["expected_target_segment_ids"] == []
    assert truth["source_fixture_truth"] is None
    assert all(not segment["expected_signal_present"] for segment in truth["segments"])
    for segment in truth["segments"]:
        first, second = segment["receivers"]
        assert first["requested_snr_db"] is None
        assert second["requested_snr_db"] is None
        assert first["achieved_prequantization_snr_db"] is None
        assert first["ci16_sha256"] != second["ci16_sha256"]


def test_multiple_target_segments_share_one_source_fixture() -> None:
    fixture = generate_paired_starlink_scan_fixture(
        _plan(),
        _case(
            target_channels=(1, 3),
            edge="upper",
            pilot_indices=tuple(range(488, 496)),
        ),
    )
    assert fixture.truth["expected_target_segment_ids"] == [
        "seg_plan_paired_starlink_fixture_01_ch1_upper",
        "seg_plan_paired_starlink_fixture_05_ch3_upper",
    ]
    assert (
        sum(segment["expected_signal_present"] for segment in fixture.truth["segments"])
        == 2
    )


def test_null_and_positive_cases_share_exact_frozen_background_across_plan_ids() -> (
    None
):
    positive = generate_paired_starlink_scan_fixture(
        _plan(plan_id="plan_positive_fixture"), _case(signal_present=True)
    )
    null = generate_paired_starlink_scan_fixture(
        _plan(plan_id="plan_null_fixture"), _case(signal_present=False)
    )

    positive_target = next(
        segment
        for segment in positive.truth["segments"]
        if segment["channel"] == 2 and segment["edge"] == "lower"
    )
    null_target = next(
        segment
        for segment in null.truth["segments"]
        if segment["channel"] == 2 and segment["edge"] == "lower"
    )
    for positive_rx, null_rx in zip(
        positive_target["receivers"], null_target["receivers"], strict=True
    ):
        assert positive_rx["noise_seed_u64"] == null_rx["noise_seed_u64"]
        assert positive_rx["base_noise_ci16_sha256"] == null_rx["ci16_sha256"]
        assert (
            positive_rx["base_noise_ci16_sha256"] == null_rx["base_noise_ci16_sha256"]
        )
        assert positive_rx["ci16_sha256"] != null_rx["ci16_sha256"]


def test_fixture_fails_closed_when_target_is_not_in_scan() -> None:
    with pytest.raises(StarlinkScanFixtureError, match="must occur exactly once"):
        generate_paired_starlink_scan_fixture(_plan(), _case(target_channels=(5,)))


def test_fixture_checks_full_pilot_support_against_analog_bandwidth() -> None:
    with pytest.raises(StarlinkScanFixtureError, match="digital and analog bandwidth"):
        generate_paired_starlink_scan_fixture(_plan(), _case(cfo_hz=70_000.0))
