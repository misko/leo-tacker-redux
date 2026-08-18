"""Canonical bounded codec for adaptive Starlink response bundles."""

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
from leo_flow.contracts.starlink_adaptive_refinement import (
    AdaptiveWindowStage,
    StarlinkAdaptiveBaseWindowV0_1,
    StarlinkAdaptiveExactWindowV0_1,
    StarlinkAdaptiveRefinementPlanV0_1,
    StarlinkAdaptiveRefinementSelectionV0_1,
)
from leo_flow.contracts.starlink_adaptive_response import (
    StarlinkAdaptiveResponseBundleV0_1,
    StarlinkAdaptiveResponsePointV0_1,
    StarlinkAdaptiveResponseStreamV0_1,
)
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod

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

MAX_STARLINK_ADAPTIVE_RESPONSE_BYTES = 256 * 1024 * 1024
STARLINK_ADAPTIVE_RESPONSE_MEDIA_TYPE = "application/json"
STARLINK_ADAPTIVE_RESPONSE_FORMAT_ID = "starlink-adaptive-response-v0.1"


class MalformedStarlinkAdaptiveResponseError(ValueError):
    pass


def encode_starlink_adaptive_response(
    bundle: StarlinkAdaptiveResponseBundleV0_1,
) -> bytes:
    payload = canonical_json_bytes(bundle)
    if len(payload) > MAX_STARLINK_ADAPTIVE_RESPONSE_BYTES:
        raise MalformedStarlinkAdaptiveResponseError(
            "adaptive response exceeds size limit"
        )
    return payload


def decode_starlink_adaptive_response(
    payload: bytes,
) -> StarlinkAdaptiveResponseBundleV0_1:
    if len(payload) > MAX_STARLINK_ADAPTIVE_RESPONSE_BYTES:
        raise MalformedStarlinkAdaptiveResponseError(
            "adaptive response exceeds size limit"
        )
    try:
        root = json.loads(payload, object_pairs_hook=_unique)
        if canonical_json_bytes(root) != payload:
            raise MalformedStarlinkAdaptiveResponseError(
                "adaptive response is not canonical"
            )
        item = _dict(root)
        result = StarlinkAdaptiveResponseBundleV0_1(
            _schema(item["schema"]),
            _str(item["analysis_id"]),
            RecordingId(_str(item["recording_id"])),
            _digest(item["recording_identity_digest"]),
            _artifact(item["timeline_ref"]),
            _artifact(item["source_suite_ref"]),
            _digest(item["request_digest"]),
            _grid(item["search_grid"]),
            _plan(item["plan"]),
            tuple(_stream(entry) for entry in _list(item["streams"])),
            _provenance(item["provenance"]),
            tuple(_str(entry) for entry in _list(item["warnings"])),
            None,
        )
        if canonical_json_bytes(result) != payload:
            raise MalformedStarlinkAdaptiveResponseError(
                "adaptive response contains unknown fields"
            )
        return result
    except MalformedStarlinkAdaptiveResponseError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedStarlinkAdaptiveResponseError(str(error)) from error


def _plan(value: object) -> StarlinkAdaptiveRefinementPlanV0_1:
    item = _dict(value)
    return StarlinkAdaptiveRefinementPlanV0_1(
        _int(item["probe_sample_count"]),
        _int(item["sentinel_stride_samples"]),
        _int(item["local_radius_samples"]),
        _int(item["local_stride_samples"]),
        _int(item["candidate_centers_per_pattern"]),
        _int(item["maximum_power_seeds"]),
        _int(item["maximum_base_windows"]),
        _int(item["maximum_exact_windows"]),
        _str(item["base_selection"]),
        _str(item["follow_up_selection"]),
    )


def _base(value: object) -> StarlinkAdaptiveBaseWindowV0_1:
    item = _dict(value)
    return StarlinkAdaptiveBaseWindowV0_1(
        _int(item["start_sample"]),
        _int(item["stop_sample"]),
        tuple(_str(entry) for entry in _list(item["selection_reasons"])),
        tuple(_int(entry) for entry in _list(item["power_seed_ranks"])),
    )


def _exact(value: object) -> StarlinkAdaptiveExactWindowV0_1:
    item = _dict(value)
    return StarlinkAdaptiveExactWindowV0_1(
        _int(item["window_index"]),
        AdaptiveWindowStage(_str(item["stage"])),
        _int(item["start_sample"]),
        _int(item["stop_sample"]),
        tuple(_str(entry) for entry in _list(item["base_selection_reasons"])),
        tuple(_artifact(entry) for entry in _list(item["selected_by_pattern_refs"])),
    )


def _selection(value: object) -> StarlinkAdaptiveRefinementSelectionV0_1:
    item = _dict(value)
    return StarlinkAdaptiveRefinementSelectionV0_1(
        _schema(item["schema"]),
        _int(item["segment_sample_count"]),
        _plan(item["plan"]),
        tuple(_artifact(entry) for entry in _list(item["pattern_refs"])),
        tuple(_base(entry) for entry in _list(item["base_windows"])),
        tuple(_exact(entry) for entry in _list(item["exact_windows"])),
        tuple(_str(entry) for entry in _list(item["warnings"])),
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


def _stream(value: object) -> StarlinkAdaptiveResponseStreamV0_1:
    item = _dict(value)
    return StarlinkAdaptiveResponseStreamV0_1(
        RadioId(_str(item["radio_id"])),
        _str(item["lnb_id"]),
        SegmentId(_str(item["segment_id"])),
        ReceiverChainId(_str(item["receiver_chain_id"])),
        _int(item["channel_number"]),
        StarlinkEdge(_str(item["edge"])),
        _number(item["sample_rate_hz"]),
        _int(item["segment_sample_count"]),
        _selection(item["selection"]),
        tuple(_point(entry) for entry in _list(item["points"])),
        _int(item["exact_covered_sample_count"]),
        _number(item["exact_coverage_fraction"]),
    )
