from __future__ import annotations

from pathlib import Path

import pytest

from leo_flow.capture.focused_monitor import materialize_focused_monitor_station
from leo_flow.capture.scan_plan import (
    starlink_edge_pilot_if_hz,
    starlink_edge_pilot_rf_hz,
)
from leo_flow.contracts.core import PlanId, canonical_digest
from leo_flow.deployments.v5_scan import DEVELOPMENT_STATION


def test_materializes_exact_ch4_lower_twenty_second_station(tmp_path: Path) -> None:
    station = materialize_focused_monitor_station(
        DEVELOPMENT_STATION,
        plan_id=PlanId("plan_focused_ch4_lower_test"),
        state_root=tmp_path / "station",
        arm_name="focused-ch4-lower-20s",
    )

    plan = station.capture_plan()
    assert canonical_digest(plan) == station.plan.plan_digest
    assert station.plan.sample_rate_hz == 2_500_000
    assert station.plan.sample_count == 50_000_000
    assert station.plan.hardware_block_samples == 100_000
    assert len(plan.activities) == 1
    assert len(plan.activities[0].segments) == 1
    segment = plan.activities[0].segments[0]
    assert segment.center_frequency_hz == 1_709_687_500
    assert segment.sample_count == 50_000_000
    assert dict(segment.tags)["channel"] == 4
    assert dict(segment.tags)["edge"] == "lower"
    assert dict(segment.tags)["rf_edge_pilot_center_hz"] == 11_459_687_500
    assert (
        starlink_edge_pilot_if_hz(4, "lower", lnb_lo_hz=9_750_000_000) == 1_709_687_500
    )
    assert starlink_edge_pilot_rf_hz(4, "lower") == 11_459_687_500
    assert station.document()["focused_tuning"] == {"channel": 4, "edge": "lower"}


def test_materializes_sixty_second_station_without_changing_rate_or_blocks(
    tmp_path: Path,
) -> None:
    station = materialize_focused_monitor_station(
        DEVELOPMENT_STATION,
        plan_id=PlanId("plan_focused_ch4_lower_60s"),
        state_root=tmp_path / "station-60s",
        arm_name="focused-ch4-lower-60s",
        duration_ns=60_000_000_000,
    )

    plan = station.capture_plan()
    assert station.plan.sample_rate_hz == 2_500_000
    assert station.plan.sample_count == 150_000_000
    assert station.plan.hardware_block_samples == 100_000
    assert plan.activities[0].segments[0].sample_count == 150_000_000
    assert canonical_digest(plan) == station.plan.plan_digest


@pytest.mark.parametrize("channel,edge", [(0, "lower"), (5, "lower"), (4, "middle")])
def test_rejects_non_low_band_focused_tuning(
    tmp_path: Path, channel: int, edge: str
) -> None:
    with pytest.raises(ValueError):
        materialize_focused_monitor_station(
            DEVELOPMENT_STATION,
            plan_id=PlanId("plan_bad_focused_test"),
            state_root=tmp_path / "station",
            arm_name="bad-focused",
            channel=channel,
            edge=edge,
        )
