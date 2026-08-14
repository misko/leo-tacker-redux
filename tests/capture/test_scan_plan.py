from __future__ import annotations

import pytest

from leo_flow.capture.scan_plan import (
    EDGE_ORDERS,
    StarlinkEdgeScanSpec,
    build_starlink_edge_scan_plan,
    edge_order_for_draw,
    starlink_edge_pilot_if_hz,
)
from leo_flow.contracts.capture import ActivityKind, GainMode, GainSetting
from leo_flow.contracts.core import PlanId, RadioId, ReceiverChainId

RECEIVERS = (ReceiverChainId("rx_v5_1"), ReceiverChainId("rx_v5_2"))


def _spec(**changes: object) -> StarlinkEdgeScanSpec:
    values = {
        "plan_id": PlanId("plan_scan_fixture"),
        "radio_id": RadioId("radio_v5"),
        "receiver_chain_ids": RECEIVERS,
        "gain": GainSetting(GainMode.AGC),
        "sample_rate_hz": 2_083_332.0,
        "bandwidth_hz": 2_000_000.0,
        "sample_count": 262_144,
        "edge_order": "L",
        "edge_order_draw_u32": 2,
        "hardware_block_samples": 262_144,
        "arm_name": "v5-qualified-1x262144",
    }
    values.update(changes)
    return StarlinkEdgeScanSpec(**values)  # type: ignore[arg-type]


def test_low_band_geometry_matches_frozen_legacy_oracle() -> None:
    expected = (
        959_687_500.0,
        1_190_312_500.0,
        1_209_687_500.0,
        1_440_312_500.0,
        1_459_687_500.0,
        1_690_312_500.0,
        1_709_687_500.0,
        1_940_312_500.0,
    )
    actual = tuple(
        starlink_edge_pilot_if_hz(channel, edge, lnb_lo_hz=9_750_000_000.0)
        for channel, edge in EDGE_ORDERS["L"]
    )
    assert actual == expected


def test_scan_plan_materializes_order_and_contains_no_dwell() -> None:
    plan = build_starlink_edge_scan_plan(_spec())

    assert len(plan.activities) == 1
    assert plan.activities[0].kind is ActivityKind.SCAN
    assert len(plan.activities[0].segments) == 8
    assert all(
        segment.receiver_chain_ids == RECEIVERS and segment.sample_count == 262_144
        for segment in plan.activities[0].segments
    )
    tags = dict(plan.experiment_tags)
    assert tags["analysis_on_capture_host"] is False
    assert tags["automatic_dwell"] is False
    assert [dict(segment.tags)["edge"] for segment in plan.activities[0].segments] == [
        "lower",
        "upper",
    ] * 4


def test_edge_order_draw_is_auditable_and_cannot_disagree() -> None:
    assert edge_order_for_draw(0) == "L"
    assert edge_order_for_draw(1) == "U"
    with pytest.raises(ValueError, match="disagrees"):
        _spec(edge_order="U")


def test_v5_scan_requires_full_pilot_band_and_block_alignment() -> None:
    with pytest.raises(ValueError, match="full edge-pilot"):
        _spec(sample_rate_hz=1_250_000.0, bandwidth_hz=1_250_000.0)
    with pytest.raises(ValueError, match="hardware block"):
        _spec(sample_count=100_000)
    with pytest.raises(ValueError, match="bandwidth"):
        _spec(sample_rate_hz=2_500_000.0, bandwidth_hz=100_000.0)
