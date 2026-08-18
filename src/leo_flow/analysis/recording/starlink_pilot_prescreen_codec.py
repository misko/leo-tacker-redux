"""Canonical bounded codec for complete-IQ pilot-prescreen evidence."""

from __future__ import annotations

import json

from leo_flow.contracts.core import (
    Digest,
    DigestAlgorithm,
    Provenance,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    SegmentId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_full_dwell_timeline_product import (
    FullDwellTimelineStreamSelectionV0_1,
)
from leo_flow.contracts.starlink_pilot_prescreen import (
    StarlinkPilotPrescreenBundleV0_1,
    StarlinkPilotPrescreenPlanV0_1,
    StarlinkPilotPrescreenStreamV0_1,
    StarlinkPilotPrescreenWindowV0_1,
)

MAXIMUM_PILOT_PRESCREEN_BYTES = 64 * 1024 * 1024
PILOT_PRESCREEN_MEDIA_TYPE = "application/json"
PILOT_PRESCREEN_FORMAT_ID = "starlink-pilot-prescreen-v0.1"


class MalformedStarlinkPilotPrescreenError(ValueError):
    pass


def encode_starlink_pilot_prescreen(
    bundle: StarlinkPilotPrescreenBundleV0_1,
) -> bytes:
    payload = canonical_json_bytes(bundle)
    if len(payload) > MAXIMUM_PILOT_PRESCREEN_BYTES:
        raise MalformedStarlinkPilotPrescreenError(
            "pilot-prescreen bundle exceeds its byte bound"
        )
    return payload


def decode_starlink_pilot_prescreen(
    payload: bytes,
) -> StarlinkPilotPrescreenBundleV0_1:
    if len(payload) > MAXIMUM_PILOT_PRESCREEN_BYTES:
        raise MalformedStarlinkPilotPrescreenError(
            "pilot-prescreen bundle exceeds its byte bound"
        )
    try:
        root = json.loads(payload, object_pairs_hook=_unique)
        if canonical_json_bytes(root) != payload:
            raise MalformedStarlinkPilotPrescreenError(
                "pilot-prescreen bundle is not canonical JSON"
            )
        item = _mapping(root)
        calibrated = item["calibrated_detection_count"]
        if calibrated is not None:
            raise MalformedStarlinkPilotPrescreenError(
                "pilot-prescreen calibrated count must be null"
            )
        result = StarlinkPilotPrescreenBundleV0_1(
            _schema(item["schema"]),
            _string(item["analysis_id"]),
            RecordingId(_string(item["recording_id"])),
            _digest(item["recording_identity_digest"]),
            _digest(item["request_digest"]),
            _plan(item["plan"]),
            tuple(_stream(value) for value in _array(item["streams"])),
            _provenance(item["provenance"]),
            _boolean(item["candidate_only"]),
            None,
            tuple(_string(value) for value in _array(item["warnings"])),
        )
        if canonical_json_bytes(result) != payload:
            raise MalformedStarlinkPilotPrescreenError(
                "pilot-prescreen bundle contains unknown fields"
            )
        return result
    except MalformedStarlinkPilotPrescreenError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedStarlinkPilotPrescreenError(str(error)) from error


def _plan(value: object) -> StarlinkPilotPrescreenPlanV0_1:
    item = _mapping(value)
    return StarlinkPilotPrescreenPlanV0_1(
        _integer(item["tile_sample_count"]),
        _integer(item["maximum_window_count_per_stream"]),
        _integer(item["maximum_periodicity_seeds_per_stream"]),
        _integer(item["maximum_power_seeds_per_stream"]),
        _string(item["selection"]),
    )


def _selection(value: object) -> FullDwellTimelineStreamSelectionV0_1:
    item = _mapping(value)
    return FullDwellTimelineStreamSelectionV0_1(
        RadioId(_string(item["radio_id"])),
        _string(item["lnb_id"]),
        SegmentId(_string(item["segment_id"])),
        ReceiverChainId(_string(item["receiver_chain_id"])),
        _integer(item["channel_number"]),
        StarlinkEdge(_string(item["edge"])),
        _number(item["sample_rate_hz"]),
        _integer(item["segment_sample_count"]),
    )


def _stream(value: object) -> StarlinkPilotPrescreenStreamV0_1:
    item = _mapping(value)
    return StarlinkPilotPrescreenStreamV0_1(
        _selection(item["selection"]),
        tuple(_window(value) for value in _array(item["windows"])),
        _integer(item["analyzed_sample_count"]),
        _number(item["coverage_fraction"]),
    )


def _window(value: object) -> StarlinkPilotPrescreenWindowV0_1:
    item = _mapping(value)
    periodicity_rank = item["periodicity_rank"]
    power_rank = item["power_rank"]
    return StarlinkPilotPrescreenWindowV0_1(
        _integer(item["window_index"]),
        _integer(item["start_sample"]),
        _integer(item["stop_sample"]),
        UtcNs(_integer(item["start_utc_ns"])),
        UtcNs(_integer(item["stop_utc_ns"])),
        _number(item["mean_power_counts_squared"]),
        _number(item["ofdm_periodicity_score"]),
        _integer(item["best_symbol_phase_sample"]),
        _integer(item["useful_symbol_lag_samples"]),
        _integer(item["total_symbol_samples"]),
        None if periodicity_rank is None else _integer(periodicity_rank),
        None if power_rank is None else _integer(power_rank),
    )


def _provenance(value: object) -> Provenance:
    item = _mapping(value)
    return Provenance(
        _string(item["producer_name"]),
        _string(item["producer_version"]),
        _string(item["git_commit"]),
        _digest(item["environment_digest"]),
        _digest(item["normalized_config_digest"]),
        tuple(_digest(value) for value in _array(item["input_digests"])),
        tuple(_digest(value) for value in _array(item["dependency_digests"])),
        UtcNs(_integer(item["started_utc_ns"])),
        UtcNs(_integer(item["completed_utc_ns"])),
        _string(item["host_class"]),
    )


def _schema(value: object) -> SchemaRef:
    item = _mapping(value)
    version = _mapping(item["version"])
    return SchemaRef(
        _string(item["schema_id"]),
        SchemaVersion(_integer(version["major"]), _integer(version["minor"])),
    )


def _digest(value: object) -> Digest:
    item = _mapping(value)
    return Digest(DigestAlgorithm(_string(item["algorithm"])), _string(item["value"]))


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MalformedStarlinkPilotPrescreenError(
                "pilot-prescreen bundle contains duplicate keys"
            )
        result[key] = value
    return result


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise MalformedStarlinkPilotPrescreenError("expected object")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise MalformedStarlinkPilotPrescreenError("expected array")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise MalformedStarlinkPilotPrescreenError("expected string")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedStarlinkPilotPrescreenError("expected integer")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MalformedStarlinkPilotPrescreenError("expected number")
    return float(value)


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise MalformedStarlinkPilotPrescreenError("expected boolean")
    return value
