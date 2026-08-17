"""Cluster calibrated stream decisions without double-counting evidence rows."""

from __future__ import annotations

from collections.abc import Sequence

from leo_flow.contracts._validation import require_finite
from leo_flow.contracts.core import V0_1, SchemaRef, UtcNs, canonical_digest
from leo_flow.contracts.starlink_events import (
    StarlinkBeaconEventV0_1,
    StarlinkCalibratedDetectionV0_1,
    StarlinkCoincidenceBasis,
)


def cluster_starlink_beacon_events_v0_1(
    detections: Sequence[StarlinkCalibratedDetectionV0_1],
    *,
    maximum_gap_ns: int,
    maximum_cfo_span_hz: float,
) -> tuple[StarlinkBeaconEventV0_1, ...]:
    """Cluster channel/edge/time/CFO tracks; receivers and radios corroborate."""

    if isinstance(maximum_gap_ns, bool) or not isinstance(maximum_gap_ns, int):
        raise TypeError("maximum_gap_ns must be an integer")
    if maximum_gap_ns < 0:
        raise ValueError("maximum_gap_ns must be non-negative")
    require_finite(maximum_cfo_span_hz, "maximum_cfo_span_hz")
    if maximum_cfo_span_hz < 0:
        raise ValueError("maximum_cfo_span_hz must be non-negative")
    unique: dict[str, StarlinkCalibratedDetectionV0_1] = {}
    for detection in detections:
        existing = unique.get(detection.candidate_id)
        if existing is not None and existing != detection:
            raise ValueError("one candidate has conflicting calibrated detections")
        unique[detection.candidate_id] = detection
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            item.interval_start_utc_ns,
            item.channel_number,
            item.edge.value,
            item.winning_cfo_hz,
            item.candidate_id,
        ),
    )
    groups: list[list[StarlinkCalibratedDetectionV0_1]] = []
    for detection in ordered:
        compatible = [
            group
            for group in groups
            if _compatible(
                group,
                detection,
                maximum_gap_ns=maximum_gap_ns,
                maximum_cfo_span_hz=maximum_cfo_span_hz,
            )
        ]
        if compatible:
            compatible[0].append(detection)
        else:
            groups.append([detection])
    return tuple(_event(group) for group in groups)


def _compatible(
    group: list[StarlinkCalibratedDetectionV0_1],
    detection: StarlinkCalibratedDetectionV0_1,
    *,
    maximum_gap_ns: int,
    maximum_cfo_span_hz: float,
) -> bool:
    first = group[0]
    if (
        detection.channel_number != first.channel_number
        or detection.edge is not first.edge
    ):
        return False
    stop = max(item.interval_stop_utc_ns for item in group)
    if detection.interval_start_utc_ns > stop + maximum_gap_ns:
        return False
    cfos = [item.winning_cfo_hz for item in group]
    return (
        max(max(cfos), detection.winning_cfo_hz)
        - min(min(cfos), detection.winning_cfo_hz)
        <= maximum_cfo_span_hz
    )


def _event(
    group: list[StarlinkCalibratedDetectionV0_1],
) -> StarlinkBeaconEventV0_1:
    ordered = sorted(group, key=lambda item: item.candidate_id)
    radios = tuple(sorted({item.radio_id for item in ordered}))
    receivers = tuple(sorted({item.receiver_chain_id for item in ordered}))
    basis = (
        StarlinkCoincidenceBasis.SOFTWARE_COORDINATED_MULTI_RADIO
        if len(radios) > 1
        else StarlinkCoincidenceBasis.INTRA_RADIO_SIMULTANEOUS
        if len(receivers) > 1
        else StarlinkCoincidenceBasis.SINGLE_STREAM
    )
    identity = canonical_digest(
        {
            "channel_number": ordered[0].channel_number,
            "edge": ordered[0].edge.value,
            "candidate_ids": tuple(item.candidate_id for item in ordered),
        }
    ).value
    return StarlinkBeaconEventV0_1(
        SchemaRef(StarlinkBeaconEventV0_1.SCHEMA_ID, V0_1),
        f"slbeaconevent_{identity[:32]}",
        ordered[0].channel_number,
        ordered[0].edge,
        UtcNs(min(item.interval_start_utc_ns for item in ordered)),
        UtcNs(max(item.interval_stop_utc_ns for item in ordered)),
        min(item.winning_cfo_hz for item in ordered),
        max(item.winning_cfo_hz for item in ordered),
        tuple(item.ref for item in ordered),
        tuple(item.candidate_id for item in ordered),
        radios,
        receivers,
        basis,
        "not_evaluated",
    )
