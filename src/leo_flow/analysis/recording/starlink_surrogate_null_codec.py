"""Canonical bounded codec for paired Starlink surrogate evidence."""

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
from leo_flow.contracts.starlink_surrogate_null import (
    StarlinkPairedMethodNullV0_1,
    StarlinkPairedSurrogateEvidenceV0_1,
    StarlinkPatternDetectionV0_1,
    StarlinkPatternMethodEvidenceV0_1,
    StarlinkPatternSearchMode,
    StarlinkSearchGridV0_1,
    StarlinkSearchPatternRole,
    StarlinkSearchPatternV0_1,
)

MAX_PAIRED_SURROGATE_EVIDENCE_BYTES = 16 * 1024 * 1024
PAIRED_SURROGATE_MEDIA_TYPE = "application/json"
PAIRED_SURROGATE_FORMAT_ID = "starlink-paired-surrogate-evidence-v0.1"


class MalformedPairedSurrogateEvidenceError(ValueError):
    pass


def encode_paired_surrogate_evidence(
    evidence: StarlinkPairedSurrogateEvidenceV0_1,
) -> bytes:
    payload = canonical_json_bytes(evidence)
    if len(payload) > MAX_PAIRED_SURROGATE_EVIDENCE_BYTES:
        raise MalformedPairedSurrogateEvidenceError(
            "paired surrogate evidence exceeds size limit"
        )
    return payload


def decode_paired_surrogate_evidence(
    data: bytes,
) -> StarlinkPairedSurrogateEvidenceV0_1:
    if len(data) > MAX_PAIRED_SURROGATE_EVIDENCE_BYTES:
        raise MalformedPairedSurrogateEvidenceError(
            "paired surrogate evidence exceeds size limit"
        )
    try:
        value = json.loads(data, object_pairs_hook=_unique)
        if canonical_json_bytes(value) != data:
            raise MalformedPairedSurrogateEvidenceError(
                "paired surrogate evidence bytes are not canonical"
            )
        root = _mapping(value)
        result = StarlinkPairedSurrogateEvidenceV0_1(
            _schema(root["schema"]),
            _string(root["analysis_id"]),
            _detection(root["exact"]),
            tuple(_detection(item) for item in _array(root["surrogates"])),
            tuple(_null(item) for item in _array(root["method_nulls"])),
            _digest(root["codebook_digest"]),
            _integer(root["minimum_recommended_surrogates"]),
            _boolean(root["candidate_only"]),
            tuple(_string(item) for item in _array(root["warnings"])),
        )
        if canonical_json_bytes(result) != data:
            raise MalformedPairedSurrogateEvidenceError(
                "paired surrogate evidence has unknown or noncanonical fields"
            )
        return result
    except MalformedPairedSurrogateEvidenceError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedPairedSurrogateEvidenceError(str(error)) from error


def _detection(value: object) -> StarlinkPatternDetectionV0_1:
    item = _mapping(value)
    return StarlinkPatternDetectionV0_1(
        _schema(item["schema"]),
        _string(item["detection_id"]),
        RecordingId(_string(item["recording_id"])),
        _digest(item["recording_identity_digest"]),
        SegmentId(_string(item["segment_id"])),
        ReceiverChainId(_string(item["receiver_chain_id"])),
        StarlinkEdge(_string(item["edge"])),
        _number(item["sample_rate_hz"]),
        _integer(item["probe_sample_count"]),
        _digest(item["input_digest"]),
        _search_grid(item["search_grid"]),
        _pattern(item["pattern"]),
        tuple(_method(entry) for entry in _array(item["methods"])),
        _provenance(item["provenance"]),
        _boolean(item["candidate_only"]),
    )


def _search_grid(value: object) -> StarlinkSearchGridV0_1:
    item = _mapping(value)
    return StarlinkSearchGridV0_1(
        _artifact(item["config_ref"]),
        tuple(_integer(entry) for entry in _array(item["epoch_hypotheses_samples"])),
        tuple(_number(entry) for entry in _array(item["coarse_cfo_hypotheses_hz"])),
        tuple(
            _number(entry) for entry in _array(item["glrt_residual_cfo_hypotheses_hz"])
        ),
        tuple(_integer(entry) for entry in _array(item["acquire_symbols"])),
        tuple(_integer(entry) for entry in _array(item["verify_symbols"])),
        _integer(item["maximum_probe_samples"]),
        _integer(item["maximum_outer_search_cells"]),
        _integer(item["maximum_effective_search_cells"]),
        _integer(item["maximum_frame_summaries"]),
    )


def _pattern(value: object) -> StarlinkSearchPatternV0_1:
    item = _mapping(value)
    seed = item["generator_seed"]
    index = item["codebook_index"]
    return StarlinkSearchPatternV0_1(
        _schema(item["schema"]),
        _string(item["pattern_id"]),
        StarlinkSearchPatternRole(_string(item["role"])),
        _artifact(item["template_ref"]),
        StarlinkEdge(_string(item["edge"])),
        tuple(_integer(entry) for entry in _array(item["pilot_subcarrier_indices"])),
        _integer(item["first_pilot_symbol"]),
        _integer(item["last_pilot_symbol"]),
        _number(item["frame_rate_hz"]),
        _number(item["sample_rate_hz"]),
        _integer(item["frame_sample_count"]),
        _number(item["template_energy"]),
        _digest(item["qpsk_state_matrix_digest"]),
        _string(item["generator_id"]),
        None if seed is None else _integer(seed),
        None if index is None else _integer(index),
        _boolean(item["data_independent"]),
    )


def _method(value: object) -> StarlinkPatternMethodEvidenceV0_1:
    item = _mapping(value)
    split = item["symbol_split_digest"]
    return StarlinkPatternMethodEvidenceV0_1(
        _schema(item["schema"]),
        StarlinkDetectorMethod(_string(item["method"])),
        _artifact(item["algorithm_ref"]),
        _artifact(item["config_ref"]),
        _digest(item["input_digest"]),
        _pattern(item["pattern"]),
        _digest(item["search_plan_digest"]),
        _digest(item["search_identity_digest"]),
        StarlinkPatternSearchMode(_string(item["search_mode"])),
        StarlinkDetectorMethod(_string(item["selection_method"])),
        _integer(item["effective_search_cell_count"]),
        _integer(item["winning_epoch_sample"]),
        _number(item["winning_coarse_cfo_hz"]),
        _number(item["winning_residual_cfo_hz"]),
        _number(item["score"]),
        _summary(item["frames"]),
        tuple(_integer(entry) for entry in _array(item["pilot_symbol_indices"])),
        _string(item["symbol_set_role"]),
        None if split is None else _digest(split),
    )


def _null(value: object) -> StarlinkPairedMethodNullV0_1:
    item = _mapping(value)
    return StarlinkPairedMethodNullV0_1(
        StarlinkDetectorMethod(_string(item["method"])),
        _number(item["target_score"]),
        tuple(_number(entry) for entry in _array(item["surrogate_scores"])),
        _number(item["empirical_upper_tail_probability"]),
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
        raise MalformedPairedSurrogateEvidenceError("expected object")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise MalformedPairedSurrogateEvidenceError("expected array")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise MalformedPairedSurrogateEvidenceError("expected string")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedPairedSurrogateEvidenceError("expected integer")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MalformedPairedSurrogateEvidenceError("expected number")
    return float(value)


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise MalformedPairedSurrogateEvidenceError("expected boolean")
    return value


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MalformedPairedSurrogateEvidenceError("duplicate object key")
        result[key] = value
    return result
