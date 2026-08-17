"""Canonical bounded JSON codec for edge-pilot constellation evidence v0.1."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping

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
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod
from leo_flow.contracts.starlink_pilot_constellation import (
    StarlinkPilotConstellationEvidenceV0_1,
    StarlinkPilotConstellationPointV0_1,
    StarlinkPilotSubcarrierSummaryV0_1,
)

MAX_STARLINK_PILOT_CONSTELLATION_BYTES = 4 * 1024 * 1024
STARLINK_PILOT_CONSTELLATION_MEDIA_TYPE = "application/json"
STARLINK_PILOT_CONSTELLATION_FORMAT_ID = "starlink-pilot-constellation-v0.1"


class MalformedStarlinkPilotConstellationError(ValueError):
    pass


def encode_starlink_pilot_constellation(
    evidence: StarlinkPilotConstellationEvidenceV0_1,
) -> bytes:
    payload = canonical_json_bytes(evidence)
    if len(payload) > MAX_STARLINK_PILOT_CONSTELLATION_BYTES:
        raise MalformedStarlinkPilotConstellationError(
            "pilot constellation exceeds size limit"
        )
    return payload


def decode_starlink_pilot_constellation(
    data: bytes,
) -> StarlinkPilotConstellationEvidenceV0_1:
    if len(data) > MAX_STARLINK_PILOT_CONSTELLATION_BYTES:
        raise MalformedStarlinkPilotConstellationError(
            "pilot constellation exceeds size limit"
        )
    try:
        value = json.loads(data, object_pairs_hook=_unique)
        if canonical_json_bytes(value) != data:
            raise MalformedStarlinkPilotConstellationError(
                "pilot constellation bytes are not canonical"
            )
        root = _mapping(value)
        _keys(root, StarlinkPilotConstellationEvidenceV0_1.__dataclass_fields__)
        return StarlinkPilotConstellationEvidenceV0_1(
            _schema(root["schema"]),
            _string(root["analysis_id"]),
            RecordingId(_string(root["recording_id"])),
            _digest(root["recording_identity_digest"]),
            SegmentId(_string(root["segment_id"])),
            ReceiverChainId(_string(root["receiver_chain_id"])),
            StarlinkEdge(_string(root["edge"])),
            _number(root["sample_rate_hz"]),
            _integer(root["probe_sample_count"]),
            _string(root["source_suite_analysis_id"]),
            _digest(root["source_suite_digest"]),
            _digest(root["source_suite_identity_digest"]),
            StarlinkDetectorMethod(_string(root["selection_method"])),
            _digest(root["acquire_search_identity_digest"]),
            _artifact(root["acquire_algorithm_ref"]),
            _artifact(root["acquire_config_ref"]),
            _artifact(root["exact_template_ref"]),
            _integer(root["winning_epoch_sample"]),
            _number(root["winning_coarse_cfo_hz"]),
            _number(root["winning_residual_cfo_hz"]),
            _number(root["residual_cfo_refinement_hz"]),
            _integer(root["complete_frame_count"]),
            _number(root["effective_frame_count"]),
            _number(root["stacking_gain_db"]),
            _integer(root["observation_count"]),
            _number(root["hard_symbol_accuracy"]),
            _number(root["random_chance_accuracy"]),
            _number(root["rms_evm"]),
            _number(root["median_equalized_magnitude"]),
            _number(root["soft_mean_confidence"]),
            _number(root["soft_mean_expected_probability"]),
            _number(root["soft_mean_entropy_bits"]),
            _number(root["soft_noise_variance"]),
            _number(root["model_snr_db"]),
            tuple(_subcarrier(item) for item in _array(root["subcarriers"])),
            tuple(_point(item) for item in _array(root["points"])),
            _string(root["point_selection"]),
            _provenance(root["provenance"]),
            _boolean(root["candidate_only"]),
            _boolean(root["known_synchronization_pilot"]),
            _boolean(root["payload_decoded"]),
            tuple(_string(item) for item in _array(root["reason_codes"])),
        )
    except MalformedStarlinkPilotConstellationError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedStarlinkPilotConstellationError(str(error)) from error


def _point(value: object) -> StarlinkPilotConstellationPointV0_1:
    item = _mapping(value)
    _keys(item, StarlinkPilotConstellationPointV0_1.__dataclass_fields__)
    return StarlinkPilotConstellationPointV0_1(
        _integer(item["symbol_index"]),
        _integer(item["subcarrier_index"]),
        _integer(item["expected_state"]),
        _integer(item["hard_state"]),
        _number(item["i"]),
        _number(item["q"]),
        _boolean(item["correct"]),
        _number(item["confidence"]),
        _number(item["expected_probability"]),
        _number(item["entropy_bits"]),
    )


def _subcarrier(value: object) -> StarlinkPilotSubcarrierSummaryV0_1:
    item = _mapping(value)
    _keys(item, StarlinkPilotSubcarrierSummaryV0_1.__dataclass_fields__)
    return StarlinkPilotSubcarrierSummaryV0_1(
        _integer(item["subcarrier_index"]),
        _number(item["offset_from_edge_center_hz"]),
        _number(item["hard_symbol_accuracy"]),
        _number(item["rms_evm"]),
        _number(item["channel_magnitude"]),
        _number(item["channel_phase_deg"]),
    )


def _artifact(value: object) -> ArtifactRef:
    item = _mapping(value)
    _keys(item, {"artifact_id": None, "digest": None, "schema": None})
    return ArtifactRef(
        _string(item["artifact_id"]),
        _digest(item["digest"]),
        None if item["schema"] is None else _schema(item["schema"]),
    )


def _provenance(value: object) -> Provenance:
    item = _mapping(value)
    _keys(item, Provenance.__dataclass_fields__)
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
    _keys(item, {"schema_id": None, "version": None})
    version = _mapping(item["version"])
    _keys(version, {"major": None, "minor": None})
    return SchemaRef(
        _string(item["schema_id"]),
        SchemaVersion(_integer(version["major"]), _integer(version["minor"])),
    )


def _digest(value: object) -> Digest:
    item = _mapping(value)
    _keys(item, {"algorithm": None, "value": None})
    return Digest(DigestAlgorithm(_string(item["algorithm"])), _string(item["value"]))


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MalformedStarlinkPilotConstellationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _keys(value: Mapping[str, object], expected: Collection[str]) -> None:
    names = set(expected)
    if set(value) != names:
        raise MalformedStarlinkPilotConstellationError(
            f"unexpected fields: expected {sorted(names)}, got {sorted(value)}"
        )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise MalformedStarlinkPilotConstellationError("expected object")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise MalformedStarlinkPilotConstellationError("expected array")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise MalformedStarlinkPilotConstellationError("expected string")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedStarlinkPilotConstellationError("expected integer")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MalformedStarlinkPilotConstellationError("expected number")
    return float(value)


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise MalformedStarlinkPilotConstellationError("expected boolean")
    return value
