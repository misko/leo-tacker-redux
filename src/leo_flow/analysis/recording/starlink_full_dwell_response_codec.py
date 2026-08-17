"""Canonical codec for additive full-dwell detector-response bundles."""

from __future__ import annotations

import json

from leo_flow.contracts.core import (
    ArtifactRef,
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
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod
from leo_flow.contracts.starlink_full_dwell_response import (
    StarlinkFullDwellPlanV0_1,
    StarlinkFullDwellPointV0_1,
    StarlinkFullDwellPrescreenWindowV0_1,
    StarlinkFullDwellResponseBundleV0_1,
    StarlinkFullDwellStreamResponseV0_1,
    StarlinkFullDwellSurrogateV0_1,
    StarlinkFullDwellWinnerV0_1,
    StarlinkWindowTier,
)
from leo_flow.contracts.starlink_surrogate_null import (
    StarlinkPatternSearchMode,
    StarlinkSearchGridV0_1,
)

MAX_STARLINK_FULL_DWELL_RESPONSE_BYTES = 256 * 1024 * 1024
STARLINK_FULL_DWELL_RESPONSE_MEDIA_TYPE = "application/json"
STARLINK_FULL_DWELL_RESPONSE_FORMAT_ID = "starlink-full-dwell-response-v0.1"


class MalformedStarlinkFullDwellResponseError(ValueError):
    pass


def encode_starlink_full_dwell_response(
    bundle: StarlinkFullDwellResponseBundleV0_1,
) -> bytes:
    data = canonical_json_bytes(bundle)
    if len(data) > MAX_STARLINK_FULL_DWELL_RESPONSE_BYTES:
        raise MalformedStarlinkFullDwellResponseError(
            "full-dwell response exceeds size limit"
        )
    return data


def decode_starlink_full_dwell_response(
    data: bytes,
) -> StarlinkFullDwellResponseBundleV0_1:
    if len(data) > MAX_STARLINK_FULL_DWELL_RESPONSE_BYTES:
        raise MalformedStarlinkFullDwellResponseError(
            "full-dwell response exceeds size limit"
        )
    try:
        root = json.loads(data, object_pairs_hook=_unique)
        if canonical_json_bytes(root) != data:
            raise MalformedStarlinkFullDwellResponseError(
                "full-dwell response is not canonical"
            )
        item = _dict(root)
        result = StarlinkFullDwellResponseBundleV0_1(
            _schema(item["schema"]),
            _str(item["analysis_id"]),
            RecordingId(_str(item["recording_id"])),
            _digest(item["recording_identity_digest"]),
            _artifact(item["source_suite_ref"]),
            _digest(item["source_suite_request_digest"]),
            _digest(item["request_digest"]),
            _grid(item["search_grid"]),
            _plan(item["plan"]),
            tuple(_stream(value) for value in _list(item["streams"])),
            _provenance(item["provenance"]),
            tuple(_str(value) for value in _list(item["warnings"])),
            None,
        )
        if canonical_json_bytes(result) != data:
            raise MalformedStarlinkFullDwellResponseError(
                "full-dwell response contains unknown fields"
            )
        return result
    except MalformedStarlinkFullDwellResponseError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedStarlinkFullDwellResponseError(str(error)) from error


def _plan(value: object) -> StarlinkFullDwellPlanV0_1:
    item = _dict(value)
    return StarlinkFullDwellPlanV0_1(
        _int(item["coarse_window_sample_count"]),
        _int(item["coarse_stride_samples"]),
        _int(item["fine_window_sample_count"]),
        _int(item["maximum_prescreen_window_count"]),
        _int(item["maximum_fine_window_count"]),
        _int(item["surrogate_count"]),
        _str(item["prescreen_metric"]),
        _str(item["refinement_selection_rule"]),
    )


def _stream(value: object) -> StarlinkFullDwellStreamResponseV0_1:
    item = _dict(value)
    return StarlinkFullDwellStreamResponseV0_1(
        RadioId(_str(item["radio_id"])),
        SegmentId(_str(item["segment_id"])),
        ReceiverChainId(_str(item["receiver_chain_id"])),
        _int(item["channel_number"]),
        StarlinkEdge(_str(item["edge"])),
        _number(item["sample_rate_hz"]),
        _int(item["segment_sample_count"]),
        tuple(_prescreen(entry) for entry in _list(item["prescreen_windows"])),
        tuple(_int(entry) for entry in _list(item["exact_window_starts"])),
        tuple(_point(entry) for entry in _list(item["points"])),
        _int(item["prescreen_covered_sample_count"]),
        _number(item["prescreen_coverage_fraction"]),
        _int(item["exact_covered_sample_count"]),
        _number(item["exact_coverage_fraction"]),
        _number(item["prescreen_overlap_fraction"]),
        _bool(item["refinement_is_data_adaptive"]),
    )


def _prescreen(value: object) -> StarlinkFullDwellPrescreenWindowV0_1:
    item = _dict(value)
    return StarlinkFullDwellPrescreenWindowV0_1(
        _int(item["window_index"]),
        _int(item["start_sample"]),
        _int(item["stop_sample"]),
        UtcNs(_int(item["interval_start_utc_ns"])),
        UtcNs(_int(item["interval_stop_utc_ns"])),
        _number(item["mean_complex_power"]),
        _bool(item["selected_for_exact_refinement"]),
    )


def _point(value: object) -> StarlinkFullDwellPointV0_1:
    item = _dict(value)
    return StarlinkFullDwellPointV0_1(
        RecordingId(_str(item["recording_id"])),
        RadioId(_str(item["radio_id"])),
        SegmentId(_str(item["segment_id"])),
        ReceiverChainId(_str(item["receiver_chain_id"])),
        StarlinkEdge(_str(item["edge"])),
        StarlinkDetectorMethod(_str(item["method"])),
        _int(item["window_index"]),
        StarlinkWindowTier(_str(item["tier"])),
        _int(item["start_sample"]),
        _int(item["stop_sample"]),
        UtcNs(_int(item["interval_start_utc_ns"])),
        UtcNs(_int(item["interval_stop_utc_ns"])),
        _number(item["prescreen_score"]),
        _winner(item["qin"]),
        tuple(_surrogate(entry) for entry in _list(item["surrogates"])),
        _int(item["finite_upper_tail_rank"]),
        _number(item["qin_minus_max_surrogate"]),
        _str(item["dependence_group"]),
    )


def _winner(value: object) -> StarlinkFullDwellWinnerV0_1:
    item = _dict(value)
    return StarlinkFullDwellWinnerV0_1(
        _number(item["score"]),
        _int(item["winning_epoch_sample_in_window"]),
        _int(item["winning_epoch_sample_in_segment"]),
        _number(item["winning_coarse_cfo_hz"]),
        _number(item["winning_residual_cfo_hz"]),
        _int(item["effective_search_cell_count"]),
        StarlinkPatternSearchMode(_str(item["search_mode"])),
        _str(item["aggregation"]),
    )


def _surrogate(value: object) -> StarlinkFullDwellSurrogateV0_1:
    item = _dict(value)
    return StarlinkFullDwellSurrogateV0_1(
        _int(item["codebook_index"]),
        _digest(item["template_digest"]),
        _winner(item["winner"]),
    )


def _grid(value: object) -> StarlinkSearchGridV0_1:
    item = _dict(value)
    return StarlinkSearchGridV0_1(
        _artifact(item["config_ref"]),
        tuple(_int(entry) for entry in _list(item["epoch_hypotheses_samples"])),
        tuple(_number(entry) for entry in _list(item["coarse_cfo_hypotheses_hz"])),
        tuple(
            _number(entry) for entry in _list(item["glrt_residual_cfo_hypotheses_hz"])
        ),
        tuple(_int(entry) for entry in _list(item["acquire_symbols"])),
        tuple(_int(entry) for entry in _list(item["verify_symbols"])),
        _int(item["maximum_probe_samples"]),
        _int(item["maximum_outer_search_cells"]),
        _int(item["maximum_effective_search_cells"]),
        _int(item["maximum_frame_summaries"]),
    )


def _provenance(value: object) -> Provenance:
    item = _dict(value)
    return Provenance(
        _str(item["producer_name"]),
        _str(item["producer_version"]),
        _str(item["git_commit"]),
        _digest(item["environment_digest"]),
        _digest(item["normalized_config_digest"]),
        tuple(_digest(entry) for entry in _list(item["input_digests"])),
        tuple(_digest(entry) for entry in _list(item["dependency_digests"])),
        UtcNs(_int(item["started_utc_ns"])),
        UtcNs(_int(item["completed_utc_ns"])),
        _str(item["host_class"]),
    )


def _artifact(value: object) -> ArtifactRef:
    item = _dict(value)
    return ArtifactRef(
        _str(item["artifact_id"]),
        _digest(item["digest"]),
        None if item["schema"] is None else _schema(item["schema"]),
    )


def _schema(value: object) -> SchemaRef:
    item = _dict(value)
    version = _dict(item["version"])
    return SchemaRef(
        _str(item["schema_id"]),
        SchemaVersion(_int(version["major"]), _int(version["minor"])),
    )


def _digest(value: object) -> Digest:
    item = _dict(value)
    return Digest(DigestAlgorithm(_str(item["algorithm"])), _str(item["value"]))


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MalformedStarlinkFullDwellResponseError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("expected object")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("expected array")
    return value


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected string")
    return value


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected integer")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("expected number")
    return float(value)


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("expected boolean")
    return value
