"""Materialize explicit low-band Starlink edge scan plans.

This module is planning policy, not radio execution.  Every tuning and every
experimental assignment is resolved before a plan reaches capture; capture
only executes the returned :class:`CapturePlan`.
"""

from __future__ import annotations

from dataclasses import dataclass

from leo_flow.contracts.capture import (
    ActivityKind,
    ActivityRequest,
    CapturePlan,
    GainSetting,
    SegmentRequest,
)
from leo_flow.contracts.core import (
    ActivityId,
    PlanId,
    RadioId,
    ReceiverChainId,
    SchemaRef,
    SegmentId,
)
from leo_flow.contracts.starlink_scan import (
    STARLINK_EDGE_SCAN_SCHEMA_V1,
    STARLINK_FOCUSED_MONITOR_SCHEMA_V1,
)

STARLINK_CHANNEL_BANDWIDTH_HZ = 240_000_000.0
STARLINK_CHANNEL_SPACING_HZ = 250_000_000.0
STARLINK_SUBCARRIER_SPACING_HZ = 234_375.0
STARLINK_PILOT_BANDWIDTH_HZ = 1_875_000.0
STARLINK_EDGE_PILOT_SUBCARRIERS = {
    "upper": tuple(range(488, 496)),
    "lower": tuple(range(528, 536)),
}
LOW_BAND_CHANNELS = (1, 2, 3, 4)
EDGE_ORDERS = {
    "L": tuple(
        (channel, edge) for channel in LOW_BAND_CHANNELS for edge in ("lower", "upper")
    ),
    "U": tuple(
        (channel, edge) for channel in LOW_BAND_CHANNELS for edge in ("upper", "lower")
    ),
}
SCAN_EXPERIMENT_SCHEMA = STARLINK_EDGE_SCAN_SCHEMA_V1
FOCUSED_MONITOR_SCHEMA = STARLINK_FOCUSED_MONITOR_SCHEMA_V1
CLIPPED_PILOT_NOTE = (
    "the sampled band does not contain the full edge-pilot band; do not pool "
    "this arm with arms whose pilot band fits"
)


def _checked_u32(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**32:
        raise ValueError(f"{name} must be an unsigned 32-bit integer")
    return value


def edge_order_for_draw(draw_u32: int) -> str:
    """Map an externally generated draw to one of the two auditable orders."""

    return ("L", "U")[_checked_u32(draw_u32, "edge-order draw") % 2]


def starlink_channel_center_hz(channel: int) -> float:
    if channel not in range(1, 9):
        raise ValueError("Starlink channel must lie in [1, 8]")
    return (
        10_700_000_000.0
        + STARLINK_SUBCARRIER_SPACING_HZ / 2
        + STARLINK_CHANNEL_SPACING_HZ * (channel - 0.5)
    )


def subcarrier_offset_hz(index: int) -> float:
    if index not in range(1024):
        raise ValueError("subcarrier index must lie in [0, 1024)")
    signed = index if index < 512 else index - 1024
    return signed * STARLINK_SUBCARRIER_SPACING_HZ


def edge_pilot_center_offset_hz(edge: str) -> float:
    try:
        indices = STARLINK_EDGE_PILOT_SUBCARRIERS[edge]
    except KeyError as error:
        raise ValueError("edge must be lower or upper") from error
    return sum(subcarrier_offset_hz(index) for index in indices) / len(indices)


def starlink_edge_pilot_if_hz(channel: int, edge: str, *, lnb_lo_hz: float) -> float:
    if lnb_lo_hz <= 0:
        raise ValueError("LNB local oscillator must be positive")
    value = (
        starlink_channel_center_hz(channel)
        - lnb_lo_hz
        + edge_pilot_center_offset_hz(edge)
    )
    if value <= 0:
        raise ValueError("edge-pilot IF lies below the configured LNB oscillator")
    return value


def starlink_edge_pilot_rf_hz(channel: int, edge: str) -> float:
    """Return the published edge-pilot center before LNB conversion."""

    return starlink_channel_center_hz(channel) + edge_pilot_center_offset_hz(edge)


@dataclass(frozen=True)
class StarlinkEdgeScanSpec:
    """One already-decided scan arm for one paired-receiver radio."""

    plan_id: PlanId
    radio_id: RadioId
    receiver_chain_ids: tuple[ReceiverChainId, ReceiverChainId]
    gain: GainSetting
    sample_rate_hz: float
    bandwidth_hz: float
    sample_count: int
    edge_order: str
    lnb_lo_hz: float = 9_750_000_000.0
    edge_order_draw_u32: int | None = None
    arm_name: str = "fixed"
    hardware_block_samples: int | None = None
    allow_clipped_pilot: bool = False

    def __post_init__(self) -> None:
        if self.edge_order not in EDGE_ORDERS:
            raise ValueError("edge order must be L or U")
        if len(set(self.receiver_chain_ids)) != 2:
            raise ValueError("edge scan requires two distinct receiver chains")
        if self.sample_rate_hz <= 0 or self.bandwidth_hz <= 0:
            raise ValueError("scan rate and bandwidth must be positive")
        if (
            isinstance(self.sample_count, bool)
            or not isinstance(self.sample_count, int)
            or self.sample_count <= 0
        ):
            raise ValueError("scan sample count must be positive")
        if not self.allow_clipped_pilot:
            if self.sample_rate_hz < STARLINK_PILOT_BANDWIDTH_HZ:
                raise ValueError("scan rate does not contain the full edge-pilot band")
            if self.bandwidth_hz < STARLINK_PILOT_BANDWIDTH_HZ:
                raise ValueError(
                    "scan bandwidth does not contain the full edge-pilot band"
                )
        if self.bandwidth_hz > self.sample_rate_hz:
            raise ValueError("scan bandwidth cannot exceed sample rate")
        if self.edge_order_draw_u32 is not None:
            draw = _checked_u32(self.edge_order_draw_u32, "edge-order draw")
            if edge_order_for_draw(draw) != self.edge_order:
                raise ValueError("edge order disagrees with its recorded draw")
        if self.hardware_block_samples is not None:
            if (
                isinstance(self.hardware_block_samples, bool)
                or not isinstance(self.hardware_block_samples, int)
                or self.hardware_block_samples <= 0
            ):
                raise ValueError("hardware block size must be positive")
            if self.sample_count % self.hardware_block_samples:
                raise ValueError("scan sample count must align to the hardware block")
        if not self.arm_name:
            raise ValueError("scan arm name cannot be empty")


def build_starlink_edge_scan_plan(spec: StarlinkEdgeScanSpec) -> CapturePlan:
    """Return an eight-segment scan whose collection order is fully explicit."""

    limiting_bandwidth_hz = min(spec.sample_rate_hz, spec.bandwidth_hz)
    pilot_guard_hz = (limiting_bandwidth_hz - STARLINK_PILOT_BANDWIDTH_HZ) / 2
    pilot_band_outside_hz = max(
        0.0, STARLINK_PILOT_BANDWIDTH_HZ - limiting_bandwidth_hz
    )
    pilot_band_clipped = pilot_band_outside_hz > 0
    pilot_tags: dict[str, object] = {
        "pilot_bandwidth_hz": STARLINK_PILOT_BANDWIDTH_HZ,
        "pilot_guard_hz": pilot_guard_hz,
        "pilot_band_fits": not pilot_band_clipped,
    }
    if pilot_band_clipped:
        pilot_tags.update(
            {
                "pilot_band_clipped": True,
                "pilot_band_outside_hz": pilot_band_outside_hz,
                "pilot_band_outside_fraction": (
                    pilot_band_outside_hz / STARLINK_PILOT_BANDWIDTH_HZ
                ),
                "pilot_band_note": CLIPPED_PILOT_NOTE,
            }
        )
    segments = tuple(
        SegmentRequest.create(
            segment_id=SegmentId(f"seg_{spec.plan_id}_{index:02d}_ch{channel}_{edge}"),
            center_frequency_hz=starlink_edge_pilot_if_hz(
                channel, edge, lnb_lo_hz=spec.lnb_lo_hz
            ),
            sample_rate_hz=spec.sample_rate_hz,
            bandwidth_hz=spec.bandwidth_hz,
            receiver_chain_ids=spec.receiver_chain_ids,
            gain=spec.gain,
            sample_count=spec.sample_count,
            tags={
                "scan_schema": SCAN_EXPERIMENT_SCHEMA,
                "tuning_index": index,
                "channel": channel,
                "edge": edge,
                "edge_order": spec.edge_order,
                "lnb_lo_hz": spec.lnb_lo_hz,
                "rf_edge_pilot_center_hz": starlink_edge_pilot_rf_hz(channel, edge),
                "arm_name": spec.arm_name,
                **pilot_tags,
            },
        )
        for index, (channel, edge) in enumerate(EDGE_ORDERS[spec.edge_order])
    )
    experiment_tags: dict[str, object] = {
        "scan_schema": SCAN_EXPERIMENT_SCHEMA,
        "purpose": "raw-edge-pilot-scan-for-offline-analysis",
        "edge_order": spec.edge_order,
        "arm_name": spec.arm_name,
        "lnb_lo_hz": spec.lnb_lo_hz,
        "analysis_on_capture_host": False,
        "automatic_dwell": False,
    }
    if pilot_band_clipped:
        experiment_tags.update(pilot_tags)
    if spec.edge_order_draw_u32 is not None:
        experiment_tags["edge_order_draw_u32"] = spec.edge_order_draw_u32
        experiment_tags["edge_order_assignment"] = "draw modulo 2"
    return CapturePlan(
        schema=SchemaRef(CapturePlan.SCHEMA_ID),
        plan_id=spec.plan_id,
        radio_id=spec.radio_id,
        receiver_chain_ids=spec.receiver_chain_ids,
        activities=(
            ActivityRequest(
                ActivityId(f"act_{spec.plan_id}_scan"),
                ActivityKind.SCAN,
                segments,
            ),
        ),
        experiment_tags=tuple(sorted(experiment_tags.items())),
    )


def build_starlink_focused_monitor_plan(
    spec: StarlinkEdgeScanSpec, *, channel: int, edge: str
) -> CapturePlan:
    """Return one explicit edge-pilot segment for sustained monitoring.

    This is deliberately separate from the published eight-tuning scan plan:
    narrowing a monitor must not change the meaning of an existing scan arm.
    """

    if channel not in LOW_BAND_CHANNELS:
        raise ValueError("focused monitor channel must lie in [1, 4]")
    if edge not in STARLINK_EDGE_PILOT_SUBCARRIERS:
        raise ValueError("focused monitor edge must be lower or upper")
    limiting_bandwidth_hz = min(spec.sample_rate_hz, spec.bandwidth_hz)
    pilot_guard_hz = (limiting_bandwidth_hz - STARLINK_PILOT_BANDWIDTH_HZ) / 2
    segment = SegmentRequest.create(
        segment_id=SegmentId(f"seg_{spec.plan_id}_ch{channel}_{edge}"),
        center_frequency_hz=starlink_edge_pilot_if_hz(
            channel, edge, lnb_lo_hz=spec.lnb_lo_hz
        ),
        sample_rate_hz=spec.sample_rate_hz,
        bandwidth_hz=spec.bandwidth_hz,
        receiver_chain_ids=spec.receiver_chain_ids,
        gain=spec.gain,
        sample_count=spec.sample_count,
        tags={
            "scan_schema": FOCUSED_MONITOR_SCHEMA,
            "tuning_index": 0,
            "channel": channel,
            "edge": edge,
            "lnb_lo_hz": spec.lnb_lo_hz,
            "rf_edge_pilot_center_hz": starlink_edge_pilot_rf_hz(channel, edge),
            "arm_name": spec.arm_name,
            "pilot_bandwidth_hz": STARLINK_PILOT_BANDWIDTH_HZ,
            "pilot_guard_hz": pilot_guard_hz,
            "pilot_band_fits": True,
        },
    )
    return CapturePlan(
        schema=SchemaRef(CapturePlan.SCHEMA_ID),
        plan_id=spec.plan_id,
        radio_id=spec.radio_id,
        receiver_chain_ids=spec.receiver_chain_ids,
        activities=(
            ActivityRequest(
                ActivityId(f"act_{spec.plan_id}_focused_monitor"),
                ActivityKind.DWELL,
                (segment,),
            ),
        ),
        experiment_tags=tuple(
            sorted(
                {
                    "scan_schema": FOCUSED_MONITOR_SCHEMA,
                    "purpose": "raw-single-edge-pilot-monitor-for-offline-analysis",
                    "channel": channel,
                    "edge": edge,
                    "arm_name": spec.arm_name,
                    "lnb_lo_hz": spec.lnb_lo_hz,
                    "analysis_on_capture_host": False,
                    "automatic_dwell": False,
                }.items()
            )
        ),
    )
