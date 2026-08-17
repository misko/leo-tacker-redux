"""Canonical bounded codec for durable v0.2 Starlink detector-suite bundles."""

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
    StarlinkDetectorMethodEvidenceV0_2,
    StarlinkDetectorSuiteBundleV0_2,
    StarlinkFrameScoreSummaryV0_2,
    StarlinkPssSssAcquisitionEvidenceV0_2,
    StarlinkSamplingStratum,
    StarlinkSearchMode,
)
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteRecordingBundleV0_2,
    StarlinkSuiteRecordingState,
)

MAX_STARLINK_SUITE_BUNDLE_BYTES = 16 * 1024 * 1024
STARLINK_SUITE_MEDIA_TYPE = "application/json"
STARLINK_SUITE_FORMAT_ID = "starlink-detector-suite-recording-bundle-v0.2"


class MalformedStarlinkSuiteBundleError(ValueError):
    pass


def encode_starlink_suite_bundle(
    bundle: StarlinkDetectorSuiteRecordingBundleV0_2,
) -> bytes:
    payload = canonical_json_bytes(bundle)
    if len(payload) > MAX_STARLINK_SUITE_BUNDLE_BYTES:
        raise MalformedStarlinkSuiteBundleError(
            "detector-suite bundle exceeds size limit"
        )
    return payload


def decode_starlink_suite_bundle(
    data: bytes,
) -> StarlinkDetectorSuiteRecordingBundleV0_2:
    if len(data) > MAX_STARLINK_SUITE_BUNDLE_BYTES:
        raise MalformedStarlinkSuiteBundleError(
            "detector-suite bundle exceeds size limit"
        )
    try:
        value = json.loads(data, object_pairs_hook=_unique)
        if canonical_json_bytes(value) != data:
            raise MalformedStarlinkSuiteBundleError(
                "detector-suite bytes are not canonical"
            )
        root = _mapping(value)
        return StarlinkDetectorSuiteRecordingBundleV0_2(
            _schema(root["schema"]),
            _string(root["analysis_id"]),
            RecordingId(_string(root["recording_id"])),
            _digest(root["recording_identity_digest"]),
            StarlinkSuiteRecordingState(_string(root["state"])),
            tuple(_suite(item) for item in _array(root["suites"])),
            tuple(_string(item) for item in _array(root["reason_codes"])),
            None
            if root["calibrated_detection_count"] is None
            else _integer(root["calibrated_detection_count"]),
        )
    except MalformedStarlinkSuiteBundleError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedStarlinkSuiteBundleError(str(error)) from error


def _suite(value: object) -> StarlinkDetectorSuiteBundleV0_2:
    item = _mapping(value)
    pss = item["pss_sss_acquisition"]
    return StarlinkDetectorSuiteBundleV0_2(
        _schema(item["schema"]),
        _string(item["analysis_id"]),
        RecordingId(_string(item["recording_id"])),
        _digest(item["recording_identity_digest"]),
        SegmentId(_string(item["segment_id"])),
        ReceiverChainId(_string(item["receiver_chain_id"])),
        StarlinkEdge(_string(item["edge"])),
        _number(item["sample_rate_hz"]),
        _integer(item["probe_sample_count"]),
        StarlinkSamplingStratum(_string(item["sampling_stratum"])),
        _digest(item["suite_identity_digest"]),
        _digest(item["symbol_split_digest"]),
        tuple(_method(entry) for entry in _array(item["methods"])),
        None if pss is None else _pss(pss),
        _provenance(item["provenance"]),
        _boolean(item["candidates_only"]),
        tuple(_string(entry) for entry in _array(item["warnings"])),
    )


def _method(value: object) -> StarlinkDetectorMethodEvidenceV0_2:
    item = _mapping(value)
    return StarlinkDetectorMethodEvidenceV0_2(
        _schema(item["schema"]),
        StarlinkDetectorMethod(_string(item["method"])),
        _artifact(item["algorithm_ref"]),
        _artifact(item["config_ref"]),
        _artifact(item["exact_template_ref"]),
        _artifact(item["conditioned_control_template_ref"]),
        _digest(item["search_identity_digest"]),
        StarlinkSearchMode(_string(item["search_mode"])),
        StarlinkDetectorMethod(_string(item["selection_method"])),
        _integer(item["effective_search_cell_count"]),
        _integer(item["winning_epoch_sample"]),
        _number(item["winning_coarse_cfo_hz"]),
        _number(item["winning_residual_cfo_hz"]),
        _number(item["reported_score"]),
        _number(item["conditioned_exact_score"]),
        _number(item["conditioned_control_score"]),
        _number(item["exact_minus_control_margin"]),
        _summary(item["exact_frames"]),
        _summary(item["control_frames"]),
        tuple(_integer(entry) for entry in _array(item["pilot_symbol_indices"])),
        _string(item["symbol_set_role"]),
        None
        if item["symbol_split_digest"] is None
        else _digest(item["symbol_split_digest"]),
        _string(item["control_conditioning"]),
        _boolean(item["candidate_only"]),
        tuple(_string(entry) for entry in _array(item["reason_codes"])),
    )


def _summary(value: object) -> StarlinkFrameScoreSummaryV0_2:
    item = _mapping(value)
    return StarlinkFrameScoreSummaryV0_2(
        _number(item["mean_score"]),
        _number(item["maximum_score"]),
        _integer(item["support"]),
    )


def _pss(value: object) -> StarlinkPssSssAcquisitionEvidenceV0_2:
    item = _mapping(value)
    return StarlinkPssSssAcquisitionEvidenceV0_2(
        _schema(item["schema"]),
        _artifact(item["template_ref"]),
        _digest(item["search_identity_digest"]),
        _integer(item["search_cell_count"]),
        _integer(item["winning_epoch_sample"]),
        _number(item["winning_doppler_hz"]),
        _number(item["searched_score"]),
        _number(item["conditioned_score"]),
        _integer(item["frame_support"]),
        _number(item["captured_template_energy_fraction"]),
        _boolean(item["supporting_only"]),
        tuple(_string(entry) for entry in _array(item["reason_codes"])),
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


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MalformedStarlinkSuiteBundleError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise MalformedStarlinkSuiteBundleError("expected object")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise MalformedStarlinkSuiteBundleError("expected array")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise MalformedStarlinkSuiteBundleError("expected string")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedStarlinkSuiteBundleError("expected integer")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MalformedStarlinkSuiteBundleError("expected number")
    return float(value)


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise MalformedStarlinkSuiteBundleError("expected boolean")
    return value
