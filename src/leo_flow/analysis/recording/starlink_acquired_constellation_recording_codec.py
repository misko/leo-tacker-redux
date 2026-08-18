"""Canonical codec for recording-level acquired-QAM v0.3 bundles."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping

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
from leo_flow.contracts.starlink_acquired_constellation import (
    StarlinkAcquiredPilotConstellationEvidenceV0_3,
)
from leo_flow.contracts.starlink_acquired_constellation_pipeline import (
    StarlinkAcquiredConstellationOverallV0_3,
    StarlinkAcquiredConstellationRecordingBundleV0_3,
    StarlinkAcquiredConstellationStreamV0_3,
    StarlinkAcquiredConstellationWindowV0_3,
)
from leo_flow.contracts.starlink_acquisition import (
    StarlinkAcquisitionBundleV0_3,
    StarlinkAcquisitionCandidateV0_3,
)
from leo_flow.contracts.starlink_pilot_constellation import (
    StarlinkPilotConstellationPointV0_1,
    StarlinkPilotSubcarrierSummaryV0_1,
)

MAX_STARLINK_ACQUIRED_CONSTELLATION_RECORDING_BYTES = 64 * 1024 * 1024
STARLINK_ACQUIRED_CONSTELLATION_RECORDING_MEDIA_TYPE = "application/json"
STARLINK_ACQUIRED_CONSTELLATION_RECORDING_FORMAT_ID = (
    "starlink-acquired-constellation-recording-v0.3"
)


class MalformedStarlinkAcquiredConstellationRecordingError(ValueError):
    pass


def encode_starlink_acquired_constellation_recording(
    bundle: StarlinkAcquiredConstellationRecordingBundleV0_3,
) -> bytes:
    payload = canonical_json_bytes(bundle)
    if len(payload) > MAX_STARLINK_ACQUIRED_CONSTELLATION_RECORDING_BYTES:
        raise MalformedStarlinkAcquiredConstellationRecordingError(
            "acquired-QAM recording exceeds size limit"
        )
    return payload


def decode_starlink_acquired_constellation_recording(
    data: bytes,
) -> StarlinkAcquiredConstellationRecordingBundleV0_3:
    if len(data) > MAX_STARLINK_ACQUIRED_CONSTELLATION_RECORDING_BYTES:
        raise MalformedStarlinkAcquiredConstellationRecordingError(
            "acquired-QAM recording exceeds size limit"
        )
    try:
        value = json.loads(data, object_pairs_hook=_unique)
        if canonical_json_bytes(value) != data:
            raise MalformedStarlinkAcquiredConstellationRecordingError(
                "acquired-QAM recording is not canonical"
            )
        root = _mapping(value)
        _keys(
            root, StarlinkAcquiredConstellationRecordingBundleV0_3.__dataclass_fields__
        )
        result = StarlinkAcquiredConstellationRecordingBundleV0_3(
            _schema(root["schema"]),
            _string(root["analysis_id"]),
            RecordingId(_string(root["recording_id"])),
            _digest(root["recording_identity_digest"]),
            _artifact(root["source_suite_ref"]),
            _digest(root["source_suite_request_digest"]),
            _digest(root["request_digest"]),
            tuple(_stream(item) for item in _array(root["streams"])),
            tuple(_string(item) for item in _array(root["reason_codes"])),
            None
            if root["calibrated_detection_count"] is None
            else _integer(root["calibrated_detection_count"]),
        )
        if canonical_json_bytes(result) != data:
            raise MalformedStarlinkAcquiredConstellationRecordingError(
                "acquired-QAM recording contains unknown fields"
            )
        return result
    except MalformedStarlinkAcquiredConstellationRecordingError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedStarlinkAcquiredConstellationRecordingError(
            str(error)
        ) from error


def _stream(value: object) -> StarlinkAcquiredConstellationStreamV0_3:
    item = _mapping(value)
    _keys(item, StarlinkAcquiredConstellationStreamV0_3.__dataclass_fields__)
    return StarlinkAcquiredConstellationStreamV0_3(
        RadioId(_string(item["radio_id"])),
        SegmentId(_string(item["segment_id"])),
        ReceiverChainId(_string(item["receiver_chain_id"])),
        StarlinkEdge(_string(item["edge"])),
        _number(item["sample_rate_hz"]),
        _integer(item["segment_sample_count"]),
        tuple(_window(entry) for entry in _array(item["windows"])),
        _overall(item["overall"]),
    )


def _window(value: object) -> StarlinkAcquiredConstellationWindowV0_3:
    item = _mapping(value)
    _keys(item, StarlinkAcquiredConstellationWindowV0_3.__dataclass_fields__)
    return StarlinkAcquiredConstellationWindowV0_3(
        _integer(item["window_index"]),
        _integer(item["start_sample"]),
        _integer(item["stop_sample"]),
        UtcNs(_integer(item["interval_start_utc_ns"])),
        UtcNs(_integer(item["interval_stop_utc_ns"])),
        _acquisition(item["acquisition"]),
        _evidence(item["evidence"]),
    )


def _overall(value: object) -> StarlinkAcquiredConstellationOverallV0_3:
    item = _mapping(value)
    _keys(item, StarlinkAcquiredConstellationOverallV0_3.__dataclass_fields__)
    return StarlinkAcquiredConstellationOverallV0_3(
        _integer(item["window_count"]),
        _integer(item["complete_frame_count"]),
        _number(item["support_weighted_hard_symbol_accuracy"]),
        _number(item["support_weighted_rms_evm"]),
        _number(item["support_weighted_model_snr_db"]),
        _number(item["maximum_held_out_verify_score"]),
        _number(item["maximum_verify_minus_control_margin"]),
        _integer(item["selected_display_window_index"]),
        _string(item["derivation"]),
    )


def _acquisition(value: object) -> StarlinkAcquisitionBundleV0_3:
    item = _mapping(value)
    _keys(item, StarlinkAcquisitionBundleV0_3.__dataclass_fields__)
    return StarlinkAcquisitionBundleV0_3(
        _schema(item["schema"]),
        _string(item["analysis_id"]),
        RecordingId(_string(item["recording_id"])),
        _digest(item["recording_identity_digest"]),
        SegmentId(_string(item["segment_id"])),
        ReceiverChainId(_string(item["receiver_chain_id"])),
        _string(item["receiver_cfo_profile_id"]),
        StarlinkEdge(_string(item["edge"])),
        _number(item["sample_rate_hz"]),
        _integer(item["probe_sample_count"]),
        _artifact(item["algorithm_ref"]),
        _artifact(item["config_ref"]),
        _artifact(item["exact_template_ref"]),
        _artifact(item["conditioned_control_template_ref"]),
        _digest(item["search_identity_digest"]),
        _number(item["searched_cfo_min_hz"]),
        _number(item["searched_cfo_max_hz"]),
        _integer(item["coarse_search_cell_count"]),
        _integer(item["refinement_search_cell_count"]),
        _integer(item["peak_count_before_retention"]),
        tuple(_candidate(entry) for entry in _array(item["candidates"])),
        _integer(item["winning_candidate_rank"]),
        tuple(_integer(entry) for entry in _array(item["acquire_symbol_indices"])),
        tuple(_integer(entry) for entry in _array(item["verify_symbol_indices"])),
        _provenance(item["provenance"]),
        _boolean(item["candidates_only"]),
        tuple(_string(entry) for entry in _array(item["reason_codes"])),
    )


def _candidate(value: object) -> StarlinkAcquisitionCandidateV0_3:
    item = _mapping(value)
    _keys(item, StarlinkAcquisitionCandidateV0_3.__dataclass_fields__)
    return StarlinkAcquisitionCandidateV0_3(
        _integer(item["coarse_epoch_sample"]),
        _number(item["coarse_cfo_hz"]),
        _number(item["coarse_score"]),
        _integer(item["refined_epoch_sample"]),
        _number(item["refined_cfo_hz"]),
        _number(item["acquire_score"]),
        _number(item["verify_score"]),
        _number(item["conditioned_control_score"]),
        _number(item["verify_minus_control_margin"]),
        _integer(item["frame_support"]),
        _integer(item["rank"]),
    )


def _evidence(value: object) -> StarlinkAcquiredPilotConstellationEvidenceV0_3:
    item = _mapping(value)
    _keys(item, StarlinkAcquiredPilotConstellationEvidenceV0_3.__dataclass_fields__)
    return StarlinkAcquiredPilotConstellationEvidenceV0_3(
        _schema(item["schema"]),
        _string(item["analysis_id"]),
        RecordingId(_string(item["recording_id"])),
        _digest(item["recording_identity_digest"]),
        SegmentId(_string(item["segment_id"])),
        ReceiverChainId(_string(item["receiver_chain_id"])),
        StarlinkEdge(_string(item["edge"])),
        _number(item["sample_rate_hz"]),
        _integer(item["probe_sample_count"]),
        _artifact(item["source_suite_ref"]),
        _artifact(item["source_acquisition_ref"]),
        _digest(item["source_acquisition_search_identity_digest"]),
        _digest(item["calibration_identity_digest"]),
        _integer(item["winning_candidate_rank"]),
        _integer(item["winning_epoch_sample"]),
        _number(item["winning_cfo_hz"]),
        _number(item["held_out_verify_score"]),
        _number(item["conditioned_control_score"]),
        _number(item["verify_minus_control_margin"]),
        _artifact(item["constellation_algorithm_ref"]),
        _artifact(item["constellation_config_ref"]),
        _number(item["residual_cfo_refinement_hz"]),
        _integer(item["complete_frame_count"]),
        _number(item["effective_frame_count"]),
        _number(item["hard_symbol_accuracy"]),
        _number(item["rms_evm"]),
        _number(item["model_snr_db"]),
        tuple(_subcarrier(entry) for entry in _array(item["subcarriers"])),
        tuple(_point(entry) for entry in _array(item["points"])),
        _provenance(item["provenance"]),
        _boolean(item["candidate_only"]),
        None
        if item["calibrated_detection"] is None
        else _boolean(item["calibrated_detection"]),
        tuple(_string(entry) for entry in _array(item["reason_codes"])),
    )


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
    _keys(item, {"artifact_id", "digest", "schema"})
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
    _keys(item, {"schema_id", "version"})
    version = _mapping(item["version"])
    _keys(version, {"major", "minor"})
    return SchemaRef(
        _string(item["schema_id"]),
        SchemaVersion(_integer(version["major"]), _integer(version["minor"])),
    )


def _digest(value: object) -> Digest:
    item = _mapping(value)
    _keys(item, {"algorithm", "value"})
    return Digest(DigestAlgorithm(_string(item["algorithm"])), _string(item["value"]))


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MalformedStarlinkAcquiredConstellationRecordingError(
                f"duplicate JSON key: {key}"
            )
        result[key] = value
    return result


def _keys(value: Mapping[str, object], expected: Collection[str]) -> None:
    if set(value) != set(expected):
        raise MalformedStarlinkAcquiredConstellationRecordingError("unexpected fields")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise MalformedStarlinkAcquiredConstellationRecordingError("expected object")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise MalformedStarlinkAcquiredConstellationRecordingError("expected array")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise MalformedStarlinkAcquiredConstellationRecordingError("expected string")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedStarlinkAcquiredConstellationRecordingError("expected integer")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MalformedStarlinkAcquiredConstellationRecordingError("expected number")
    return float(value)


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise MalformedStarlinkAcquiredConstellationRecordingError("expected boolean")
    return value
