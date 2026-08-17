"""Canonical codec for additive symmetric rolled-template control bundles."""

from __future__ import annotations

import json
from collections.abc import Mapping

from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    Provenance,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    SegmentId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import (
    StarlinkDetectorMethod,
    StarlinkFrameScoreSummaryV0_2,
)
from leo_flow.contracts.starlink_full_search_control import (
    StarlinkFullSearchControlMethodEvidenceV0_1,
    StarlinkFullSearchControlMode,
    StarlinkFullSearchControlRecordingBundleV0_1,
    StarlinkFullSearchControlRecordingState,
    StarlinkFullSearchControlSuiteV0_1,
)

MAX_FULL_SEARCH_CONTROL_BUNDLE_BYTES = 16 * 1024 * 1024
FULL_SEARCH_CONTROL_MEDIA_TYPE = "application/json"
FULL_SEARCH_CONTROL_FORMAT_ID = "starlink-full-search-control-recording-bundle-v0.1"


class MalformedFullSearchControlBundleError(ValueError):
    pass


def encode_full_search_control_bundle(
    bundle: StarlinkFullSearchControlRecordingBundleV0_1,
) -> bytes:
    payload = canonical_json_bytes(bundle)
    if len(payload) > MAX_FULL_SEARCH_CONTROL_BUNDLE_BYTES:
        raise MalformedFullSearchControlBundleError(
            "full-search control bundle exceeds size limit"
        )
    return payload


def decode_full_search_control_bundle(
    data: bytes,
) -> StarlinkFullSearchControlRecordingBundleV0_1:
    if len(data) > MAX_FULL_SEARCH_CONTROL_BUNDLE_BYTES:
        raise MalformedFullSearchControlBundleError(
            "full-search control bundle exceeds size limit"
        )
    try:
        value = json.loads(data, object_pairs_hook=_unique)
        if canonical_json_bytes(value) != data:
            raise MalformedFullSearchControlBundleError(
                "full-search control bytes are not canonical"
            )
        root = _mapping(value)
        return StarlinkFullSearchControlRecordingBundleV0_1(
            _schema(root["schema"]),
            _string(root["analysis_id"]),
            RecordingId(_string(root["recording_id"])),
            _digest(root["recording_identity_digest"]),
            _digest(root["source_request_digest"]),
            StarlinkFullSearchControlRecordingState(_string(root["state"])),
            tuple(_suite(item) for item in _array(root["suites"])),
            tuple(_string(item) for item in _array(root["reason_codes"])),
        )
    except MalformedFullSearchControlBundleError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedFullSearchControlBundleError(str(error)) from error


def _suite(value: object) -> StarlinkFullSearchControlSuiteV0_1:
    item = _mapping(value)
    return StarlinkFullSearchControlSuiteV0_1(
        _schema(item["schema"]),
        _string(item["analysis_id"]),
        RecordingId(_string(item["recording_id"])),
        _digest(item["recording_identity_digest"]),
        SegmentId(_string(item["segment_id"])),
        ReceiverChainId(_string(item["receiver_chain_id"])),
        StarlinkEdge(_string(item["edge"])),
        _number(item["sample_rate_hz"]),
        _integer(item["probe_sample_count"]),
        _digest(item["suite_identity_digest"]),
        tuple(_method(entry) for entry in _array(item["methods"])),
        _provenance(item["provenance"]),
        _boolean(item["surrogate_only"]),
        tuple(_string(entry) for entry in _array(item["warnings"])),
    )


def _method(value: object) -> StarlinkFullSearchControlMethodEvidenceV0_1:
    item = _mapping(value)
    split = item["symbol_split_digest"]
    return StarlinkFullSearchControlMethodEvidenceV0_1(
        _schema(item["schema"]),
        StarlinkDetectorMethod(_string(item["method"])),
        _artifact(item["algorithm_ref"]),
        _artifact(item["config_ref"]),
        _artifact(item["rolled_template_ref"]),
        _digest(item["search_identity_digest"]),
        StarlinkFullSearchControlMode(_string(item["search_mode"])),
        StarlinkDetectorMethod(_string(item["selection_method"])),
        _integer(item["effective_search_cell_count"]),
        _integer(item["winning_epoch_sample"]),
        _number(item["winning_coarse_cfo_hz"]),
        _number(item["winning_residual_cfo_hz"]),
        _number(item["full_search_control_score"]),
        _summary(item["control_frames"]),
        tuple(_integer(entry) for entry in _array(item["pilot_symbol_indices"])),
        _string(item["symbol_set_role"]),
        None if split is None else _digest(split),
        _string(item["control_search"]),
        _boolean(item["surrogate_only"]),
        tuple(_string(entry) for entry in _array(item["reason_codes"])),
    )


def _summary(value: object) -> StarlinkFrameScoreSummaryV0_2:
    item = _mapping(value)
    return StarlinkFrameScoreSummaryV0_2(
        _number(item["mean_score"]),
        _number(item["maximum_score"]),
        _integer(item["support"]),
    )


def _artifact(value: object) -> ArtifactRef:
    item = _mapping(value)
    return ArtifactRef(
        _string(item["artifact_id"]),
        _digest(item["digest"]),
        None if item["schema"] is None else _schema(item["schema"]),
    )


def _provenance(value: object) -> Provenance:
    item = _mapping(value)
    return Provenance(
        _string(item["producer_name"]),
        _string(item["producer_version"]),
        _string(item["git_commit"]),
        _digest(item["environment_digest"]),
        _digest(item["normalized_config_digest"]),
        tuple(_digest(entry) for entry in _array(item["input_digests"])),
        tuple(_digest(entry) for entry in _array(item["dependency_digests"])),
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


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MalformedFullSearchControlBundleError("expected object")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise MalformedFullSearchControlBundleError("expected array")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise MalformedFullSearchControlBundleError("expected string")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedFullSearchControlBundleError("expected integer")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MalformedFullSearchControlBundleError("expected number")
    return float(value)


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise MalformedFullSearchControlBundleError("expected boolean")
    return value


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MalformedFullSearchControlBundleError("duplicate object key")
        result[key] = value
    return result
