"""Canonical bounded codec for prescreen-selected exact pilot responses."""

from __future__ import annotations

import json

from leo_flow.contracts.core import (
    RadioId,
    ReceiverChainId,
    RecordingId,
    SegmentId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_adaptive_response import (
    StarlinkAdaptiveResponsePointV0_1,
)
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod
from leo_flow.contracts.starlink_pilot_refinement import (
    StarlinkPilotRefinementBundleV0_1,
    StarlinkPilotRefinementSeedV0_1,
    StarlinkPilotRefinementStreamSelectionV0_1,
    StarlinkPilotRefinementStreamV0_1,
)

from .starlink_full_dwell_response_codec import (
    _artifact,
    _dict,
    _digest,
    _grid,
    _int,
    _list,
    _number,
    _provenance,
    _schema,
    _str,
    _surrogate,
    _unique,
    _winner,
)

MAX_STARLINK_PILOT_REFINEMENT_BYTES = 256 * 1024 * 1024
STARLINK_PILOT_REFINEMENT_MEDIA_TYPE = "application/json"
STARLINK_PILOT_REFINEMENT_FORMAT_ID = "starlink-pilot-refinement-v0.1"


class MalformedStarlinkPilotRefinementError(ValueError):
    pass


def encode_starlink_pilot_refinement(
    bundle: StarlinkPilotRefinementBundleV0_1,
) -> bytes:
    payload = canonical_json_bytes(bundle)
    if len(payload) > MAX_STARLINK_PILOT_REFINEMENT_BYTES:
        raise MalformedStarlinkPilotRefinementError(
            "pilot refinement exceeds size limit"
        )
    return payload


def decode_starlink_pilot_refinement(
    payload: bytes,
) -> StarlinkPilotRefinementBundleV0_1:
    if len(payload) > MAX_STARLINK_PILOT_REFINEMENT_BYTES:
        raise MalformedStarlinkPilotRefinementError(
            "pilot refinement exceeds size limit"
        )
    try:
        root = json.loads(payload, object_pairs_hook=_unique)
        if canonical_json_bytes(root) != payload:
            raise MalformedStarlinkPilotRefinementError(
                "pilot refinement is not canonical"
            )
        item = _dict(root)
        result = StarlinkPilotRefinementBundleV0_1(
            _schema(item["schema"]),
            _str(item["analysis_id"]),
            RecordingId(_str(item["recording_id"])),
            _digest(item["recording_identity_digest"]),
            _artifact(item["source_prescreen_ref"]),
            _artifact(item["source_suite_ref"]),
            _digest(item["request_digest"]),
            _grid(item["search_grid"]),
            tuple(_stream(entry) for entry in _list(item["streams"])),
            _provenance(item["provenance"]),
            tuple(_str(entry) for entry in _list(item["warnings"])),
            None,
        )
        if canonical_json_bytes(result) != payload:
            raise MalformedStarlinkPilotRefinementError(
                "pilot refinement has unknown fields"
            )
        return result
    except MalformedStarlinkPilotRefinementError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedStarlinkPilotRefinementError(str(error)) from error


def _optional_int(value: object) -> int | None:
    return None if value is None else _int(value)


def _seed(value: object) -> StarlinkPilotRefinementSeedV0_1:
    item = _dict(value)
    return StarlinkPilotRefinementSeedV0_1(
        _int(item["seed_index"]),
        _int(item["start_sample"]),
        _int(item["stop_sample"]),
        _number(item["ofdm_periodicity_score"]),
        _number(item["mean_power_counts_squared"]),
        _optional_int(item["periodicity_rank"]),
        _optional_int(item["power_rank"]),
    )


def _selection(value: object) -> StarlinkPilotRefinementStreamSelectionV0_1:
    item = _dict(value)
    return StarlinkPilotRefinementStreamSelectionV0_1(
        RadioId(_str(item["radio_id"])),
        _str(item["lnb_id"]),
        SegmentId(_str(item["segment_id"])),
        ReceiverChainId(_str(item["receiver_chain_id"])),
        _int(item["channel_number"]),
        StarlinkEdge(_str(item["edge"])),
        _number(item["sample_rate_hz"]),
        _int(item["segment_sample_count"]),
        tuple(_seed(entry) for entry in _list(item["seeds"])),
    )


def _point(value: object) -> StarlinkAdaptiveResponsePointV0_1:
    item = _dict(value)
    return StarlinkAdaptiveResponsePointV0_1(
        StarlinkDetectorMethod(_str(item["method"])),
        _int(item["window_index"]),
        _int(item["start_sample"]),
        _int(item["stop_sample"]),
        UtcNs(_int(item["interval_start_utc_ns"])),
        UtcNs(_int(item["interval_stop_utc_ns"])),
        _winner(item["qin"]),
        tuple(_surrogate(entry) for entry in _list(item["surrogates"])),
        _int(item["finite_upper_tail_rank"]),
        _number(item["qin_minus_max_surrogate"]),
    )


def _stream(value: object) -> StarlinkPilotRefinementStreamV0_1:
    item = _dict(value)
    return StarlinkPilotRefinementStreamV0_1(
        _selection(item["selection"]),
        tuple(_point(entry) for entry in _list(item["points"])),
        _int(item["exact_covered_sample_count"]),
        _number(item["exact_coverage_fraction"]),
    )
