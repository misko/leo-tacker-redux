"""Canonical bounded codec for uncalibrated Starlink candidate bundles."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import NoReturn

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
from leo_flow.contracts.starlink import (
    StarlinkEdge,
    StarlinkPilotAnalysisBundleV0_1,
    StarlinkPilotSearchCandidateV0_1,
)

MAX_STARLINK_BUNDLE_BYTES = 4 * 1024 * 1024
STARLINK_MEDIA_TYPE = "application/json"
STARLINK_FORMAT_ID = "starlink-pilot-analysis-bundle-v0.1"


class MalformedStarlinkBundleError(ValueError):
    """Candidate bytes are oversized, noncanonical, ambiguous, or invalid."""


def encode_starlink_bundle(bundle: StarlinkPilotAnalysisBundleV0_1) -> bytes:
    payload = canonical_json_bytes(bundle)
    if len(payload) > MAX_STARLINK_BUNDLE_BYTES:
        raise MalformedStarlinkBundleError("Starlink bundle exceeds size limit")
    return payload


def decode_starlink_bundle(data: bytes) -> StarlinkPilotAnalysisBundleV0_1:
    if len(data) > MAX_STARLINK_BUNDLE_BYTES:
        raise MalformedStarlinkBundleError("Starlink bundle exceeds size limit")
    try:
        document = json.loads(data, object_pairs_hook=_unique_object)
        if canonical_json_bytes(document) != data:
            _bad("Starlink bytes are not canonical JSON")
        root = _object(document, "root")
        _keys(
            root,
            {
                "schema",
                "analysis_id",
                "recording_id",
                "recording_identity_digest",
                "candidates",
                "warnings",
            },
            "root",
        )
        schema = _schema(root["schema"], "schema")
        if schema.schema_id != StarlinkPilotAnalysisBundleV0_1.SCHEMA_ID:
            _bad("unsupported durable Starlink bundle schema")
        return StarlinkPilotAnalysisBundleV0_1(
            schema,
            _string(root["analysis_id"], "analysis_id"),
            RecordingId(_string(root["recording_id"], "recording_id")),
            _digest(root["recording_identity_digest"], "recording_identity_digest"),
            tuple(
                _candidate(item, index)
                for index, item in enumerate(_array(root["candidates"], "candidates"))
            ),
            tuple(
                _string(item, "warning")
                for item in _array(root["warnings"], "warnings")
            ),
        )
    except MalformedStarlinkBundleError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedStarlinkBundleError(str(error)) from error


def _candidate(value: object, index: int) -> StarlinkPilotSearchCandidateV0_1:
    name = f"candidates[{index}]"
    item = _object(value, name)
    _keys(
        item,
        {
            "schema",
            "candidate_id",
            "recording_id",
            "recording_identity_digest",
            "segment_id",
            "receiver_chain_id",
            "edge",
            "pilot_indices",
            "algorithm_ref",
            "config_ref",
            "exact_template_ref",
            "conditioned_control_template_ref",
            "search_identity_digest",
            "sample_rate_hz",
            "probe_sample_count",
            "frame_period_samples",
            "epoch_hypotheses_samples",
            "cfo_hypotheses_hz",
            "search_cell_count",
            "winning_epoch_sample",
            "winning_cfo_hz",
            "searched_exact_score",
            "conditioned_exact_score",
            "conditioned_control_score",
            "exact_minus_control_margin",
            "frame_support",
            "control_conditioning",
            "pss_evidence_status",
            "provenance",
            "reason_codes",
        },
        name,
    )
    return StarlinkPilotSearchCandidateV0_1(
        _schema(item["schema"], f"{name}.schema"),
        _string(item["candidate_id"], f"{name}.candidate_id"),
        RecordingId(_string(item["recording_id"], f"{name}.recording_id")),
        _digest(item["recording_identity_digest"], f"{name}.recording_digest"),
        SegmentId(_string(item["segment_id"], f"{name}.segment_id")),
        ReceiverChainId(
            _string(item["receiver_chain_id"], f"{name}.receiver_chain_id")
        ),
        StarlinkEdge(_string(item["edge"], f"{name}.edge")),
        tuple(
            _integer(entry, f"{name}.pilot_indices")
            for entry in _array(item["pilot_indices"], f"{name}.pilot_indices")
        ),
        _artifact(item["algorithm_ref"], f"{name}.algorithm_ref"),
        _artifact(item["config_ref"], f"{name}.config_ref"),
        _artifact(item["exact_template_ref"], f"{name}.exact_template_ref"),
        _artifact(
            item["conditioned_control_template_ref"],
            f"{name}.conditioned_control_template_ref",
        ),
        _digest(item["search_identity_digest"], f"{name}.search_identity_digest"),
        _number(item["sample_rate_hz"], f"{name}.sample_rate_hz"),
        _integer(item["probe_sample_count"], f"{name}.probe_sample_count"),
        _number(item["frame_period_samples"], f"{name}.frame_period_samples"),
        tuple(
            _integer(entry, f"{name}.epoch_hypotheses_samples")
            for entry in _array(
                item["epoch_hypotheses_samples"],
                f"{name}.epoch_hypotheses_samples",
            )
        ),
        tuple(
            _number(entry, f"{name}.cfo_hypotheses_hz")
            for entry in _array(item["cfo_hypotheses_hz"], f"{name}.cfo_hypotheses_hz")
        ),
        _integer(item["search_cell_count"], f"{name}.search_cell_count"),
        _integer(item["winning_epoch_sample"], f"{name}.winning_epoch_sample"),
        _number(item["winning_cfo_hz"], f"{name}.winning_cfo_hz"),
        _number(item["searched_exact_score"], f"{name}.searched_exact_score"),
        _number(item["conditioned_exact_score"], f"{name}.conditioned_exact_score"),
        _number(item["conditioned_control_score"], f"{name}.conditioned_control_score"),
        _number(
            item["exact_minus_control_margin"],
            f"{name}.exact_minus_control_margin",
        ),
        _integer(item["frame_support"], f"{name}.frame_support"),
        _string(item["control_conditioning"], f"{name}.control_conditioning"),
        _string(item["pss_evidence_status"], f"{name}.pss_evidence_status"),
        _provenance(item["provenance"], f"{name}.provenance"),
        tuple(
            _string(entry, f"{name}.reason_codes")
            for entry in _array(item["reason_codes"], f"{name}.reason_codes")
        ),
    )


def _artifact(value: object, name: str) -> ArtifactRef:
    item = _object(value, name)
    _keys(item, {"artifact_id", "digest", "schema"}, name)
    return ArtifactRef(
        _string(item["artifact_id"], f"{name}.artifact_id"),
        _digest(item["digest"], f"{name}.digest"),
        None if item["schema"] is None else _schema(item["schema"], f"{name}.schema"),
    )


def _provenance(value: object, name: str) -> Provenance:
    item = _object(value, name)
    _keys(
        item,
        {
            "producer_name",
            "producer_version",
            "git_commit",
            "environment_digest",
            "normalized_config_digest",
            "input_digests",
            "dependency_digests",
            "started_utc_ns",
            "completed_utc_ns",
            "host_class",
        },
        name,
    )
    return Provenance(
        _string(item["producer_name"], f"{name}.producer_name"),
        _string(item["producer_version"], f"{name}.producer_version"),
        _string(item["git_commit"], f"{name}.git_commit"),
        _digest(item["environment_digest"], f"{name}.environment_digest"),
        _digest(item["normalized_config_digest"], f"{name}.normalized_config_digest"),
        tuple(
            _digest(entry, f"{name}.input_digest")
            for entry in _array(item["input_digests"], f"{name}.input_digests")
        ),
        tuple(
            _digest(entry, f"{name}.dependency_digest")
            for entry in _array(
                item["dependency_digests"], f"{name}.dependency_digests"
            )
        ),
        UtcNs(_integer(item["started_utc_ns"], f"{name}.started_utc_ns")),
        UtcNs(_integer(item["completed_utc_ns"], f"{name}.completed_utc_ns")),
        _string(item["host_class"], f"{name}.host_class"),
    )


def _schema(value: object, name: str) -> SchemaRef:
    item = _object(value, name)
    _keys(item, {"schema_id", "version"}, name)
    version = _object(item["version"], f"{name}.version")
    _keys(version, {"major", "minor"}, f"{name}.version")
    return SchemaRef(
        _string(item["schema_id"], f"{name}.schema_id"),
        SchemaVersion(
            _integer(version["major"], f"{name}.version.major"),
            _integer(version["minor"], f"{name}.version.minor"),
        ),
    )


def _digest(value: object, name: str) -> Digest:
    item = _object(value, name)
    _keys(item, {"algorithm", "value"}, name)
    return Digest(
        DigestAlgorithm(_string(item["algorithm"], f"{name}.algorithm")),
        _string(item["value"], f"{name}.value"),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _bad(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        _bad(f"{name} must be an object")
    return value


def _array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        _bad(f"{name} must be an array")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        _bad(f"{name} must be a string")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _bad(f"{name} must be an integer")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _bad(f"{name} must be a number")
    return float(value)


def _keys(value: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        _bad(f"{name} fields differ from schema")


def _bad(message: str) -> NoReturn:
    raise MalformedStarlinkBundleError(message)
