"""Strict canonical codec for durable receiver-agnostic CFO/QAM v0.6."""

from __future__ import annotations

import json
from typing import Any

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
from leo_flow.contracts.starlink_adaptive_calibration import AdaptivePatternRole
from leo_flow.contracts.starlink_receiver_agnostic_cfo import (
    ReceiverAgnosticCfoCellStage,
    ReceiverAgnosticCfoCellV0_6,
    ReceiverAgnosticCfoPatternV0_6,
    ReceiverAgnosticCfoQamWindowBundleV0_6,
    ReceiverAgnosticCfoSearchPlanV0_6,
    ReceiverAgnosticCfoSearchReceiptV0_6,
    ReceiverAgnosticCfoWindowV0_6,
    ReceiverAgnosticCfoWinnerV0_6,
    ReceiverAgnosticPatternQamEvidenceV0_6,
)
from leo_flow.contracts.starlink_receiver_agnostic_cfo_product import (
    ReceiverAgnosticCfoQamRecordingBundleV0_6,
    ReceiverAgnosticCfoQamRecordingPlanV0_6,
    ReceiverAgnosticCfoQamRecordingRequestV0_6,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef

MAX_RECEIVER_AGNOSTIC_CFO_QAM_REQUEST_BYTES = 512 * 1024
MAX_RECEIVER_AGNOSTIC_CFO_QAM_BUNDLE_BYTES = 128 * 1024 * 1024
RECEIVER_AGNOSTIC_CFO_QAM_MEDIA_TYPE = "application/json"
RECEIVER_AGNOSTIC_CFO_QAM_FORMAT_ID = "receiver-agnostic-cfo-qam-v0.6"


class MalformedReceiverAgnosticCfoQamError(ValueError):
    pass


def encode_receiver_agnostic_cfo_qam_request(
    request: ReceiverAgnosticCfoQamRecordingRequestV0_6,
) -> bytes:
    return _bounded_encode(request, MAX_RECEIVER_AGNOSTIC_CFO_QAM_REQUEST_BYTES)


def decode_receiver_agnostic_cfo_qam_request(
    data: bytes,
) -> ReceiverAgnosticCfoQamRecordingRequestV0_6:
    root = _decode_root(data, MAX_RECEIVER_AGNOSTIC_CFO_QAM_REQUEST_BYTES)
    try:
        item = _dict(root)
        result = ReceiverAgnosticCfoQamRecordingRequestV0_6(
            _schema(item["schema"]),
            RecordingId(_str(item["recording_id"])),
            _recording_ref(item["recording_object_ref"]),
            _recording_plan(item["plan"]),
            tuple(_window(value) for value in _list(item["windows"])),
            _schema(item["requested_output_schema"]),
        )
        _reject_unknown(result, data)
        return result
    except MalformedReceiverAgnosticCfoQamError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedReceiverAgnosticCfoQamError(str(error)) from error


def encode_receiver_agnostic_cfo_qam_bundle(
    bundle: ReceiverAgnosticCfoQamRecordingBundleV0_6,
) -> bytes:
    return _bounded_encode(bundle, MAX_RECEIVER_AGNOSTIC_CFO_QAM_BUNDLE_BYTES)


def decode_receiver_agnostic_cfo_qam_bundle(
    data: bytes,
) -> ReceiverAgnosticCfoQamRecordingBundleV0_6:
    root = _decode_root(data, MAX_RECEIVER_AGNOSTIC_CFO_QAM_BUNDLE_BYTES)
    try:
        item = _dict(root)
        result = ReceiverAgnosticCfoQamRecordingBundleV0_6(
            _schema(item["schema"]),
            _str(item["analysis_id"]),
            RecordingId(_str(item["recording_id"])),
            _digest(item["recording_identity_digest"]),
            _digest(item["request_digest"]),
            _recording_plan(item["plan"]),
            tuple(_window_bundle(value) for value in _list(item["window_products"])),
            _provenance(item["provenance"]),
            _bool(item["candidates_only"]),
            _none(item["calibrated_detection_count"]),
            tuple(_str(value) for value in _list(item["disclosures"])),
        )
        _reject_unknown(result, data)
        return result
    except MalformedReceiverAgnosticCfoQamError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedReceiverAgnosticCfoQamError(str(error)) from error


def _recording_plan(value: object) -> ReceiverAgnosticCfoQamRecordingPlanV0_6:
    item = _dict(value)
    return ReceiverAgnosticCfoQamRecordingPlanV0_6(
        _search_plan(item["search_plan"]),
        _int(item["maximum_streams"]),
        _int(item["maximum_windows_per_stream"]),
        _int(item["maximum_patterns"]),
        _int(item["maximum_pattern_evidence"]),
        _str(item["admission_mode"]),
    )


def _search_plan(value: object) -> ReceiverAgnosticCfoSearchPlanV0_6:
    item = _dict(value)
    return ReceiverAgnosticCfoSearchPlanV0_6(
        _number(item["cfo_min_hz"]),
        _number(item["cfo_max_hz"]),
        _number(item["coarse_cfo_step_hz"]),
        _number(item["local_cfo_radius_hz"]),
        _number(item["local_cfo_step_hz"]),
        _int(item["coarse_epoch_stride_samples"]),
        _int(item["local_epoch_radius_samples"]),
        _int(item["basins_per_pattern"]),
        _number(item["basin_cfo_separation_hz"]),
        _int(item["basin_epoch_separation_samples"]),
        _int(item["maximum_coarse_cells"]),
        _int(item["maximum_local_cells"]),
        _int(item["maximum_unique_cells"]),
        _int(item["maximum_pattern_evaluations"]),
        _str(item["receiver_adjustment_policy"]),
        _str(item["pattern_search_policy"]),
    )


def _window(value: object) -> ReceiverAgnosticCfoWindowV0_6:
    item = _dict(value)
    return ReceiverAgnosticCfoWindowV0_6(
        RecordingId(_str(item["recording_id"])),
        _digest(item["recording_identity_digest"]),
        RadioId(_str(item["radio_id"])),
        SegmentId(_str(item["segment_id"])),
        ReceiverChainId(_str(item["receiver_chain_id"])),
        StarlinkEdge(_str(item["edge"])),
        _number(item["sample_rate_hz"]),
        _int(item["start_sample"]),
        _int(item["stop_sample"]),
        _artifact(item["source_recording_ref"]),
        _artifact(item["source_window_ref"]),
    )


def _window_bundle(value: object) -> ReceiverAgnosticCfoQamWindowBundleV0_6:
    item = _dict(value)
    return ReceiverAgnosticCfoQamWindowBundleV0_6(
        _schema(item["schema"]),
        _str(item["analysis_id"]),
        _window(item["window"]),
        _artifact(item["search_algorithm_ref"]),
        _artifact(item["scorer_algorithm_ref"]),
        _artifact(item["qam_algorithm_ref"]),
        _artifact(item["config_ref"]),
        _receipt(item["search_receipt"]),
        tuple(_qam(value) for value in _list(item["pattern_qam"])),
        _provenance(item["provenance"]),
        _bool(item["candidates_only"]),
        _none(item["calibrated_detection_count"]),
        tuple(_str(value) for value in _list(item["disclosures"])),
    )


def _receipt(value: object) -> ReceiverAgnosticCfoSearchReceiptV0_6:
    item = _dict(value)
    return ReceiverAgnosticCfoSearchReceiptV0_6(
        _schema(item["schema"]),
        _search_plan(item["plan"]),
        _int(item["epoch_modulus_samples"]),
        tuple(_pattern(value) for value in _list(item["patterns"])),
        tuple(_cell(value) for value in _list(item["cells"])),
        tuple(_winner(value) for value in _list(item["winners"])),
        _int(item["coarse_cell_count"]),
        _int(item["local_cell_count"]),
        _int(item["unique_cell_count"]),
        _int(item["pattern_evaluation_count"]),
        _int(item["look_elsewhere_hypothesis_count"]),
        _bool(item["candidates_only"]),
        _none(item["calibrated_detection_count"]),
        tuple(_str(value) for value in _list(item["disclosures"])),
    )


def _pattern(value: object) -> ReceiverAgnosticCfoPatternV0_6:
    item = _dict(value)
    return ReceiverAgnosticCfoPatternV0_6(
        _int(item["pattern_index"]),
        AdaptivePatternRole(_str(item["role"])),
        _digest(item["template_digest"]),
    )


def _cell(value: object) -> ReceiverAgnosticCfoCellV0_6:
    item = _dict(value)
    return ReceiverAgnosticCfoCellV0_6(
        _int(item["cell_index"]),
        ReceiverAgnosticCfoCellStage(_str(item["stage"])),
        _int(item["epoch_sample"]),
        _number(item["cfo_hz"]),
        tuple(_int(value) for value in _list(item["selected_by_pattern_indices"])),
        tuple(_number(value) for value in _list(item["pattern_scores"])),
    )


def _winner(value: object) -> ReceiverAgnosticCfoWinnerV0_6:
    item = _dict(value)
    return ReceiverAgnosticCfoWinnerV0_6(
        _int(item["pattern_index"]),
        _int(item["cell_index"]),
        _int(item["epoch_sample"]),
        _number(item["cfo_hz"]),
        _number(item["score"]),
    )


def _qam(value: object) -> ReceiverAgnosticPatternQamEvidenceV0_6:
    item = _dict(value)
    return ReceiverAgnosticPatternQamEvidenceV0_6(
        _int(item["pattern_index"]),
        AdaptivePatternRole(_str(item["role"])),
        _artifact(item["template_ref"]),
        _artifact(item["control_template_ref"]),
        _winner(item["winner"]),
        _int(item["complete_frame_count"]),
        _number(item["hard_symbol_accuracy"]),
        _number(item["rms_evm"]),
        _number(item["qam_goodness"]),
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
        tuple(_digest(value) for value in _list(item["input_digests"])),
        tuple(_digest(value) for value in _list(item["dependency_digests"])),
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
        raise MalformedReceiverAgnosticCfoQamError(
            "receiver-agnostic CFO/QAM payload exceeds size limit"
        )
    return data


def _decode_root(data: bytes, maximum: int) -> object:
    if len(data) > maximum:
        raise MalformedReceiverAgnosticCfoQamError(
            "receiver-agnostic CFO/QAM payload exceeds size limit"
        )
    try:
        root = json.loads(data, object_pairs_hook=_unique)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MalformedReceiverAgnosticCfoQamError(str(error)) from error
    if canonical_json_bytes(root) != data:
        raise MalformedReceiverAgnosticCfoQamError(
            "receiver-agnostic CFO/QAM payload is not canonical"
        )
    return root


def _reject_unknown(value: object, data: bytes) -> None:
    if canonical_json_bytes(value) != data:
        raise MalformedReceiverAgnosticCfoQamError(
            "receiver-agnostic CFO/QAM payload contains unknown fields"
        )


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MalformedReceiverAgnosticCfoQamError(f"duplicate JSON key: {key}")
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


def _none(value: object) -> Any:
    if value is not None:
        raise TypeError("expected null")
    return value
