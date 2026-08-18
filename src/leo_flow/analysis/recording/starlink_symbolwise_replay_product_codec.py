"""Canonical codecs for recording-level symbolwise replay requests and bundles."""

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
from leo_flow.contracts.starlink_surrogate_null import (
    StarlinkSearchPatternRole,
    StarlinkSearchPatternV0_1,
)
from leo_flow.contracts.starlink_symbolwise_replay import (
    StarlinkReceiverFrequencyCenterV0_1,
    StarlinkSymbolwisePatternEvidenceV0_1,
    StarlinkSymbolwiseReplayBundleV0_1,
    StarlinkSymbolwiseWindowEvidenceV0_1,
)
from leo_flow.contracts.starlink_symbolwise_replay_product import (
    StarlinkSymbolwiseRecordingBundleV0_1,
    StarlinkSymbolwiseRecordingPlanV0_1,
    StarlinkSymbolwiseReplayRequestV0_1,
    StarlinkSymbolwiseReplayStreamSelectionV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef

MAX_STARLINK_SYMBOLWISE_REPLAY_REQUEST_BYTES = 256 * 1024
MAX_STARLINK_SYMBOLWISE_RECORDING_BUNDLE_BYTES = 128 * 1024 * 1024
STARLINK_SYMBOLWISE_RECORDING_MEDIA_TYPE = "application/json"
STARLINK_SYMBOLWISE_RECORDING_FORMAT_ID = "starlink-symbolwise-recording-v0.1"


class MalformedStarlinkSymbolwiseReplayError(ValueError):
    pass


def encode_starlink_symbolwise_replay_request(
    request: StarlinkSymbolwiseReplayRequestV0_1,
) -> bytes:
    return _bounded_encode(request, MAX_STARLINK_SYMBOLWISE_REPLAY_REQUEST_BYTES)


def decode_starlink_symbolwise_replay_request(
    data: bytes,
) -> StarlinkSymbolwiseReplayRequestV0_1:
    root = _decode_root(data, MAX_STARLINK_SYMBOLWISE_REPLAY_REQUEST_BYTES)
    try:
        result = _request(root)
        _reject_unknown(result, data)
        return result
    except MalformedStarlinkSymbolwiseReplayError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedStarlinkSymbolwiseReplayError(str(error)) from error


def encode_starlink_symbolwise_recording_bundle(
    bundle: StarlinkSymbolwiseRecordingBundleV0_1,
) -> bytes:
    return _bounded_encode(bundle, MAX_STARLINK_SYMBOLWISE_RECORDING_BUNDLE_BYTES)


def decode_starlink_symbolwise_recording_bundle(
    data: bytes,
) -> StarlinkSymbolwiseRecordingBundleV0_1:
    root = _decode_root(data, MAX_STARLINK_SYMBOLWISE_RECORDING_BUNDLE_BYTES)
    try:
        item = _dict(root)
        result = StarlinkSymbolwiseRecordingBundleV0_1(
            _schema(item["schema"]),
            _str(item["analysis_id"]),
            RecordingId(_str(item["recording_id"])),
            _digest(item["recording_identity_digest"]),
            _digest(item["request_digest"]),
            _plan(item["plan"]),
            tuple(_selection(value) for value in _list(item["stream_selections"])),
            tuple(_receiver_bundle(value) for value in _list(item["streams"])),
            _provenance(item["provenance"]),
            _bool(item["candidates_only"]),
            tuple(_str(value) for value in _list(item["reason_codes"])),
        )
        _reject_unknown(result, data)
        return result
    except MalformedStarlinkSymbolwiseReplayError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedStarlinkSymbolwiseReplayError(str(error)) from error


def _request(value: object) -> StarlinkSymbolwiseReplayRequestV0_1:
    item = _dict(value)
    return StarlinkSymbolwiseReplayRequestV0_1(
        _schema(item["schema"]),
        RecordingId(_str(item["recording_id"])),
        _recording_ref(item["recording_object_ref"]),
        _plan(item["plan"]),
        tuple(_selection(entry) for entry in _list(item["stream_selections"])),
        _schema(item["requested_output_schema"]),
    )


def _plan(value: object) -> StarlinkSymbolwiseRecordingPlanV0_1:
    item = _dict(value)
    return StarlinkSymbolwiseRecordingPlanV0_1(
        _number(item["dwell_duration_s"]),
        _number(item["window_duration_s"]),
        _number(item["cadence_s"]),
        _int(item["surrogate_count"]),
        _int(item["maximum_windows"]),
        _int(item["maximum_window_samples"]),
        _int(item["maximum_timing_search_cells"]),
        _int(item["maximum_refinement_search_cells"]),
        _int(item["maximum_working_bytes"]),
        _str(item["admission_mode"]),
    )


def _selection(value: object) -> StarlinkSymbolwiseReplayStreamSelectionV0_1:
    item = _dict(value)
    return StarlinkSymbolwiseReplayStreamSelectionV0_1(
        RadioId(_str(item["radio_id"])),
        SegmentId(_str(item["segment_id"])),
        ReceiverChainId(_str(item["receiver_chain_id"])),
        StarlinkEdge(_str(item["edge"])),
        _number(item["sample_rate_hz"]),
        _int(item["segment_sample_count"]),
        _frequency_center(item["frequency_center"]),
    )


def _receiver_bundle(value: object) -> StarlinkSymbolwiseReplayBundleV0_1:
    item = _dict(value)
    return StarlinkSymbolwiseReplayBundleV0_1(
        _schema(item["schema"]),
        _str(item["analysis_id"]),
        RecordingId(_str(item["recording_id"])),
        _digest(item["recording_identity_digest"]),
        SegmentId(_str(item["segment_id"])),
        ReceiverChainId(_str(item["receiver_chain_id"])),
        StarlinkEdge(_str(item["edge"])),
        _number(item["sample_rate_hz"]),
        _int(item["segment_sample_count"]),
        _frequency_center(item["frequency_center"]),
        _artifact(item["algorithm_ref"]),
        _artifact(item["config_ref"]),
        _digest(item["surrogate_codebook_digest"]),
        _int(item["window_sample_count"]),
        _int(item["cadence_sample_count"]),
        tuple(_window(entry) for entry in _list(item["windows"])),
        _int(item["analyzed_union_sample_count"]),
        _number(item["coverage_fraction"]),
        _int(item["timing_search_cell_count"]),
        _int(item["refinement_search_cell_count"]),
        _int(item["maximum_working_bytes"]),
        _provenance(item["provenance"]),
        _bool(item["candidates_only"]),
        tuple(_str(entry) for entry in _list(item["reason_codes"])),
    )


def _frequency_center(value: object) -> StarlinkReceiverFrequencyCenterV0_1:
    item = _dict(value)
    return StarlinkReceiverFrequencyCenterV0_1(
        _schema(item["schema"]),
        _str(item["calibration_id"]),
        _digest(item["hardware_epoch_digest"]),
        _digest(item["receiver_signal_path_digest"]),
        _artifact(item["source_ref"]),
        _number(item["center_cfo_hz"]),
        _str(item["reference_definition"]),
        _bool(item["data_independent"]),
    )


def _window(value: object) -> StarlinkSymbolwiseWindowEvidenceV0_1:
    item = _dict(value)
    return StarlinkSymbolwiseWindowEvidenceV0_1(
        _int(item["window_index"]),
        _int(item["start_sample"]),
        _int(item["stop_sample"]),
        _digest(item["input_digest"]),
        tuple(_pattern_evidence(entry) for entry in _list(item["patterns"])),
    )


def _pattern_evidence(value: object) -> StarlinkSymbolwisePatternEvidenceV0_1:
    item = _dict(value)
    return StarlinkSymbolwisePatternEvidenceV0_1(
        _pattern(item["pattern"]),
        _artifact(item["selection_control_template_ref"]),
        _int(item["timing_search_cell_count"]),
        _int(item["refinement_search_cell_count"]),
        _int(item["retained_candidate_count"]),
        _int(item["selected_candidate_rank"]),
        _int(item["winning_epoch_sample"]),
        _number(item["timing_coarse_cfo_hz"]),
        _number(item["timing_score"]),
        _number(item["timing_folded_median"]),
        _number(item["timing_peak_to_median"]),
        _int(item["timing_symbol_frame_support"]),
        _number(item["symbolwise_coarse_cfo_hz"]),
        _number(item["symbolwise_residual_cfo_hz"]),
        _number(item["winning_cfo_hz"]),
        _number(item["symbolwise_score"]),
        _number(item["symbolwise_control_score"]),
        _number(item["symbolwise_margin"]),
        _number(item["symbolwise_coherence"]),
        _number(item["symbolwise_control_coherence"]),
        _number(item["conditioned_score"]),
        _number(item["conditioned_control_score"]),
        _number(item["conditioned_margin"]),
        _number(item["conditioned_maximum_score"]),
        _number(item["conditioned_control_maximum_score"]),
        _int(item["frame_support"]),
        _int(item["symbol_match_count"]),
        _number(item["selection_score"]),
    )


def _pattern(value: object) -> StarlinkSearchPatternV0_1:
    item = _dict(value)
    return StarlinkSearchPatternV0_1(
        _schema(item["schema"]),
        _str(item["pattern_id"]),
        StarlinkSearchPatternRole(_str(item["role"])),
        _artifact(item["template_ref"]),
        StarlinkEdge(_str(item["edge"])),
        tuple(_int(entry) for entry in _list(item["pilot_subcarrier_indices"])),
        _int(item["first_pilot_symbol"]),
        _int(item["last_pilot_symbol"]),
        _number(item["frame_rate_hz"]),
        _number(item["sample_rate_hz"]),
        _int(item["frame_sample_count"]),
        _number(item["template_energy"]),
        _digest(item["qpsk_state_matrix_digest"]),
        _str(item["generator_id"]),
        None if item["generator_seed"] is None else _int(item["generator_seed"]),
        None if item["codebook_index"] is None else _int(item["codebook_index"]),
        _bool(item["data_independent"]),
    )


def _recording_ref(value: object) -> RecordingObjectRef:
    item = _dict(value)
    return RecordingObjectRef(
        RecordingId(_str(item["recording_id"])),
        _object_ref(item["data_object"]),
        _object_ref(item["metadata_object"]),
        _digest(item["manifest_digest"]),
    )


def _object_ref(value: object) -> ObjectRef:
    item = _dict(value)
    return ObjectRef(
        _digest(item["digest"]),
        _int(item["byte_count"]),
        _str(item["media_type"]),
        _str(item["format_id"]),
        _str(item["locator"]),
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


def _bounded_encode(value: object, maximum: int) -> bytes:
    data = canonical_json_bytes(value)
    if len(data) > maximum:
        raise MalformedStarlinkSymbolwiseReplayError(
            "symbolwise replay payload exceeds size limit"
        )
    return data


def _decode_root(data: bytes, maximum: int) -> object:
    if len(data) > maximum:
        raise MalformedStarlinkSymbolwiseReplayError(
            "symbolwise replay payload exceeds size limit"
        )
    try:
        root = json.loads(data, object_pairs_hook=_unique)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MalformedStarlinkSymbolwiseReplayError(str(error)) from error
    if canonical_json_bytes(root) != data:
        raise MalformedStarlinkSymbolwiseReplayError(
            "symbolwise replay payload is not canonical"
        )
    return root


def _reject_unknown(value: object, data: bytes) -> None:
    if canonical_json_bytes(value) != data:
        raise MalformedStarlinkSymbolwiseReplayError(
            "symbolwise replay payload contains unknown fields"
        )


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MalformedStarlinkSymbolwiseReplayError(f"duplicate JSON key: {key}")
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
