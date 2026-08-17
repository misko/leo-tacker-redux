"""Private materialization for one-frequency synchronized V5 monitoring."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from leo_flow.contracts.capture import CapturePlan, GainMode, GainSetting
from leo_flow.contracts.core import PlanId, canonical_digest

from .scan_plan import StarlinkEdgeScanSpec, build_starlink_focused_monitor_plan
from .v5_station import V5CaptureState, V5CaptureStation, V5ScanDefinition


@dataclass(frozen=True, slots=True)
class FocusedV5CaptureStation(V5CaptureStation):
    """A station whose immutable plan contains exactly one channel/edge."""

    focused_channel: int
    focused_edge: str

    def capture_plan(self) -> CapturePlan:
        return build_starlink_focused_monitor_plan(
            StarlinkEdgeScanSpec(
                plan_id=self.plan.plan_id,
                radio_id=self.radio.radio_id,
                receiver_chain_ids=self.radio.receiver_chain_ids,
                gain=GainSetting(GainMode.AGC),
                sample_rate_hz=self.plan.sample_rate_hz,
                bandwidth_hz=self.plan.bandwidth_hz,
                sample_count=self.plan.sample_count,
                edge_order=self.plan.edge_order,
                lnb_lo_hz=self.plan.lnb_lo_hz,
                edge_order_draw_u32=self.plan.edge_order_draw_u32,
                arm_name=self.plan.arm_name,
                hardware_block_samples=self.plan.hardware_block_samples,
            ),
            channel=self.focused_channel,
            edge=self.focused_edge,
        )

    def document(self) -> dict[str, object]:
        value = V5CaptureStation.document(self)
        value["focused_tuning"] = {
            "channel": self.focused_channel,
            "edge": self.focused_edge,
        }
        return value


def materialize_focused_monitor_station(
    base: V5CaptureStation,
    *,
    plan_id: PlanId,
    state_root: Path,
    arm_name: str,
    channel: int = 4,
    edge: str = "lower",
    sample_rate_hz: int = 2_500_000,
    bandwidth_hz: int = 2_500_000,
    duration_ns: int = 20_000_000_000,
    hardware_block_duration_ns: int = 40_000_000,
) -> FocusedV5CaptureStation:
    """Bind a qualified station identity to one exact sustained monitor plan."""

    if duration_ns <= 0 or duration_ns % 1_000_000_000:
        raise ValueError("focused duration must be a positive whole number of seconds")
    sample_count = sample_rate_hz * duration_ns // 1_000_000_000
    hardware_block_samples = (
        sample_rate_hz * hardware_block_duration_ns // 1_000_000_000
    )
    if sample_count % hardware_block_samples:
        raise ValueError("focused duration must align to the hardware block")
    provisional = V5ScanDefinition(
        plan_id=plan_id,
        plan_digest=canonical_digest({"provisional": str(plan_id)}),
        sample_rate_hz=float(sample_rate_hz),
        bandwidth_hz=float(bandwidth_hz),
        sample_count=sample_count,
        edge_order="L",
        edge_order_draw_u32=0,
        arm_name=arm_name,
        lnb_lo_hz=9_750_000_000.0,
        hardware_block_samples=hardware_block_samples,
    )
    spec = StarlinkEdgeScanSpec(
        plan_id=plan_id,
        radio_id=base.radio.radio_id,
        receiver_chain_ids=base.radio.receiver_chain_ids,
        gain=GainSetting(GainMode.AGC),
        sample_rate_hz=provisional.sample_rate_hz,
        bandwidth_hz=provisional.bandwidth_hz,
        sample_count=provisional.sample_count,
        edge_order=provisional.edge_order,
        lnb_lo_hz=provisional.lnb_lo_hz,
        edge_order_draw_u32=provisional.edge_order_draw_u32,
        arm_name=provisional.arm_name,
        hardware_block_samples=provisional.hardware_block_samples,
    )
    plan = build_starlink_focused_monitor_plan(spec, channel=channel, edge=edge)
    focused_plan = replace(provisional, plan_digest=canonical_digest(plan))
    state = V5CaptureState(
        state_root=state_root,
        recording_root=state_root / "recordings",
        spool_database=state_root / "capture-spool.sqlite3",
        cas_root=base.state.cas_root,
        lock_path=state_root / "instance.lock",
        mode_lock_path=base.state.mode_lock_path,
        minimum_free_bytes=base.state.minimum_free_bytes,
        require_cas_mount=base.state.require_cas_mount,
    )
    return FocusedV5CaptureStation(
        station_id=base.station_id,
        radio=base.radio,
        hardware_snapshot_id=base.hardware_snapshot_id,
        clock_status=base.clock_status,
        capture_implementation=base.capture_implementation,
        runtime_manifest=base.runtime_manifest,
        runtime_manifest_digest=base.runtime_manifest_digest,
        expected_runtime=base.expected_runtime,
        plan=focused_plan,
        state=state,
        focused_channel=channel,
        focused_edge=edge,
    )
