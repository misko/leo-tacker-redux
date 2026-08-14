"""Strict bounded canonical codec for immutable tracking-input snapshots."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any, NoReturn

from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    DatasetSnapshotId,
    Digest,
    DigestAlgorithm,
    EphemerisSnapshotId,
    FeatureId,
    FeatureSetId,
    HardwareSnapshotId,
    Provenance,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    StationId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.ephemeris import (
    EphemerisSelection,
    EphemerisSelectionPolicy,
    EphemerisSnapshotRef,
    EphemerisSource,
    RecordingEphemerisLink,
    RecordingInterval,
)
from leo_flow.contracts.features import Covariance
from leo_flow.contracts.hardware import (
    HardwareMetadataSnapshotRef,
    RecordingHardwareLink,
)
from leo_flow.contracts.tracking_input import (
    MAX_CALIBRATION_SOURCE_REFS,
    MAX_TRACKING_INPUT_ENTRIES,
    AbsoluteRfMeasurementEvidence,
    DurableDatasetIdentity,
    FeatureSetIdentity,
    PredictionCovarianceEvidence,
    ReceiverCalibrationEvidence,
    RfReferenceFrame,
    TrackingInputEntry,
    TrackingInputSnapshot,
)

MAX_TRACKING_INPUT_BYTES = 128 * 1024 * 1024


class MalformedTrackingInputError(ValueError):
    """Tracking evidence is oversized, ambiguous, noncanonical, or invalid."""


def encode_tracking_input(snapshot: TrackingInputSnapshot) -> bytes:
    payload = canonical_json_bytes(snapshot)
    if len(payload) > MAX_TRACKING_INPUT_BYTES:
        raise MalformedTrackingInputError("tracking input exceeds size limit")
    return payload


def decode_tracking_input(data: bytes) -> TrackingInputSnapshot:
    if len(data) > MAX_TRACKING_INPUT_BYTES:
        raise MalformedTrackingInputError("tracking input exceeds size limit")
    try:
        document = json.loads(data, object_pairs_hook=_unique_object)
        if canonical_json_bytes(document) != data:
            _bad("tracking input bytes are not canonical JSON")
        root = _object(document, "root")
        _keys(
            root,
            {
                "schema",
                "snapshot_id",
                "durable_dataset",
                "builder_ref",
                "selector_ref",
                "provenance",
                "entries",
                "membership_digest",
                "snapshot_digest",
            },
            "root",
        )
        entries_raw = _array(root["entries"], "entries")
        if len(entries_raw) > MAX_TRACKING_INPUT_ENTRIES:
            _bad("tracking input has too many entries")
        return TrackingInputSnapshot(
            schema=_schema(root["schema"], "schema"),
            snapshot_id=_string(root["snapshot_id"], "snapshot_id"),
            durable_dataset=_dataset(root["durable_dataset"]),
            builder_ref=_artifact(root["builder_ref"], "builder_ref"),
            selector_ref=_artifact(root["selector_ref"], "selector_ref"),
            provenance=_provenance(root["provenance"]),
            entries=tuple(
                _entry(value, index) for index, value in enumerate(entries_raw)
            ),
            membership_digest=_digest(root["membership_digest"], "membership_digest"),
            snapshot_digest=_digest(root["snapshot_digest"], "snapshot_digest"),
        )
    except MalformedTrackingInputError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedTrackingInputError(str(error)) from error


def _dataset(value: object) -> DurableDatasetIdentity:
    item = _object(value, "durable_dataset")
    _keys(
        item,
        {"snapshot_id", "feature_membership_digest", "snapshot_digest"},
        "durable_dataset",
    )
    return DurableDatasetIdentity(
        DatasetSnapshotId(_string(item["snapshot_id"], "dataset.snapshot_id")),
        _digest(item["feature_membership_digest"], "feature_membership_digest"),
        _digest(item["snapshot_digest"], "dataset.snapshot_digest"),
    )


def _entry(value: object, index: int) -> TrackingInputEntry:
    name = f"entries[{index}]"
    item = _object(value, name)
    _keys(
        item,
        {
            "feature_set",
            "recording_identity_digest",
            "recording_interval",
            "hardware_link",
            "ephemeris_link",
            "measurement",
            "calibration",
            "prediction",
        },
        name,
    )
    return TrackingInputEntry(
        feature_set=_feature_set(item["feature_set"], f"{name}.feature_set"),
        recording_identity_digest=_digest(
            item["recording_identity_digest"], f"{name}.recording_identity_digest"
        ),
        recording_interval=_interval(
            item["recording_interval"], f"{name}.recording_interval"
        ),
        hardware_link=_hardware_link(item["hardware_link"], f"{name}.hardware_link"),
        ephemeris_link=_ephemeris_link(
            item["ephemeris_link"], f"{name}.ephemeris_link"
        ),
        measurement=_measurement(item["measurement"], f"{name}.measurement"),
        calibration=_calibration(item["calibration"], f"{name}.calibration"),
        prediction=_prediction(item["prediction"], f"{name}.prediction"),
    )


def _feature_set(value: object, name: str) -> FeatureSetIdentity:
    item = _object(value, name)
    _keys(
        item,
        {
            "feature_set_id",
            "analysis_run_id",
            "bundle_digest",
            "bundle_byte_count",
            "bundle_media_type",
            "bundle_format_id",
        },
        name,
    )
    return FeatureSetIdentity(
        FeatureSetId(_string(item["feature_set_id"], f"{name}.feature_set_id")),
        AnalysisRunId(_string(item["analysis_run_id"], f"{name}.analysis_run_id")),
        _digest(item["bundle_digest"], f"{name}.bundle_digest"),
        _integer(item["bundle_byte_count"], f"{name}.bundle_byte_count"),
        _string(item["bundle_media_type"], f"{name}.bundle_media_type"),
        _string(item["bundle_format_id"], f"{name}.bundle_format_id"),
    )


def _measurement(value: object, name: str) -> AbsoluteRfMeasurementEvidence:
    item = _object(value, name)
    _keys(
        item,
        {
            "feature_id",
            "recording_id",
            "receiver_chain_id",
            "midpoint_utc_ns",
            "reference_frame",
            "value",
            "basis",
            "units",
            "covariance",
        },
        name,
    )
    return AbsoluteRfMeasurementEvidence(
        FeatureId(_string(item["feature_id"], f"{name}.feature_id")),
        RecordingId(_string(item["recording_id"], f"{name}.recording_id")),
        ReceiverChainId(
            _string(item["receiver_chain_id"], f"{name}.receiver_chain_id")
        ),
        UtcNs(_integer(item["midpoint_utc_ns"], f"{name}.midpoint_utc_ns")),
        RfReferenceFrame(_string(item["reference_frame"], f"{name}.reference_frame")),
        _float_pair(item["value"], f"{name}.value"),
        _string_pair(item["basis"], f"{name}.basis"),
        _string_pair(item["units"], f"{name}.units"),
        _covariance(item["covariance"], f"{name}.covariance"),
    )


def _calibration(value: object, name: str) -> ReceiverCalibrationEvidence:
    item = _object(value, name)
    _keys(
        item,
        {
            "calibration_ref",
            "receiver_chain_id",
            "hardware_snapshot_ref",
            "station_id",
            "validity",
            "value",
            "basis",
            "units",
            "covariance",
            "source_refs",
        },
        name,
    )
    source_values = _array(item["source_refs"], f"{name}.source_refs")
    if len(source_values) > MAX_CALIBRATION_SOURCE_REFS:
        _bad(f"{name}.source_refs has too many entries")
    return ReceiverCalibrationEvidence(
        _artifact(item["calibration_ref"], f"{name}.calibration_ref"),
        ReceiverChainId(
            _string(item["receiver_chain_id"], f"{name}.receiver_chain_id")
        ),
        _hardware_ref(item["hardware_snapshot_ref"], f"{name}.hardware_snapshot_ref"),
        StationId(_string(item["station_id"], f"{name}.station_id")),
        _interval(item["validity"], f"{name}.validity"),
        _float_pair(item["value"], f"{name}.value"),
        _string_pair(item["basis"], f"{name}.basis"),
        _string_pair(item["units"], f"{name}.units"),
        _covariance(item["covariance"], f"{name}.covariance"),
        tuple(
            _artifact(source, f"{name}.source_refs[{index}]")
            for index, source in enumerate(source_values)
        ),
    )


def _prediction(value: object, name: str) -> PredictionCovarianceEvidence:
    item = _object(value, name)
    _keys(item, {"policy_ref", "basis", "units", "covariance"}, name)
    return PredictionCovarianceEvidence(
        _artifact(item["policy_ref"], f"{name}.policy_ref"),
        _string_pair(item["basis"], f"{name}.basis"),
        _string_pair(item["units"], f"{name}.units"),
        _covariance(item["covariance"], f"{name}.covariance"),
    )


def _hardware_link(value: object, name: str) -> RecordingHardwareLink:
    item = _object(value, name)
    _keys(
        item,
        {
            "link_id",
            "recording_id",
            "recording_identity_digest",
            "hardware_snapshot_ref",
            "link_digest",
        },
        name,
    )
    return RecordingHardwareLink(
        _string(item["link_id"], f"{name}.link_id"),
        RecordingId(_string(item["recording_id"], f"{name}.recording_id")),
        _digest(
            item["recording_identity_digest"],
            f"{name}.recording_identity_digest",
        ),
        _hardware_ref(item["hardware_snapshot_ref"], f"{name}.hardware_snapshot_ref"),
        _digest(item["link_digest"], f"{name}.link_digest"),
    )


def _ephemeris_link(value: object, name: str) -> RecordingEphemerisLink:
    item = _object(value, name)
    _keys(
        item,
        {
            "link_id",
            "recording_id",
            "recording_identity_digest",
            "recording_interval",
            "scope",
            "selection",
            "link_digest",
        },
        name,
    )
    return RecordingEphemerisLink(
        _string(item["link_id"], f"{name}.link_id"),
        RecordingId(_string(item["recording_id"], f"{name}.recording_id")),
        _digest(
            item["recording_identity_digest"],
            f"{name}.recording_identity_digest",
        ),
        _interval(item["recording_interval"], f"{name}.recording_interval"),
        _string(item["scope"], f"{name}.scope"),
        _selection(item["selection"], f"{name}.selection"),
        _digest(item["link_digest"], f"{name}.link_digest"),
    )


def _selection(value: object, name: str) -> EphemerisSelection:
    item = _object(value, name)
    _keys(
        item,
        {"source", "policy", "policy_ref", "snapshot_ref", "as_of_utc_ns"},
        name,
    )
    return EphemerisSelection(
        EphemerisSource(_string(item["source"], f"{name}.source")),
        EphemerisSelectionPolicy(_string(item["policy"], f"{name}.policy")),
        _artifact(item["policy_ref"], f"{name}.policy_ref"),
        _ephemeris_ref(item["snapshot_ref"], f"{name}.snapshot_ref"),
        UtcNs(_integer(item["as_of_utc_ns"], f"{name}.as_of_utc_ns")),
    )


def _hardware_ref(value: object, name: str) -> HardwareMetadataSnapshotRef:
    item = _object(value, name)
    _keys(item, {"snapshot_id", "digest"}, name)
    return HardwareMetadataSnapshotRef(
        HardwareSnapshotId(_string(item["snapshot_id"], f"{name}.snapshot_id")),
        _digest(item["digest"], f"{name}.digest"),
    )


def _ephemeris_ref(value: object, name: str) -> EphemerisSnapshotRef:
    item = _object(value, name)
    _keys(
        item,
        {"snapshot_id", "source", "raw_digest", "normalized_digest"},
        name,
    )
    return EphemerisSnapshotRef(
        EphemerisSnapshotId(_string(item["snapshot_id"], f"{name}.snapshot_id")),
        EphemerisSource(_string(item["source"], f"{name}.source")),
        _digest(item["raw_digest"], f"{name}.raw_digest"),
        _digest(item["normalized_digest"], f"{name}.normalized_digest"),
    )


def _interval(value: object, name: str) -> RecordingInterval:
    item = _object(value, name)
    _keys(item, {"started_utc_ns", "finished_utc_ns"}, name)
    return RecordingInterval(
        UtcNs(_integer(item["started_utc_ns"], f"{name}.started_utc_ns")),
        UtcNs(_integer(item["finished_utc_ns"], f"{name}.finished_utc_ns")),
    )


def _covariance(value: object, name: str) -> Covariance:
    item = _object(value, name)
    _keys(item, {"basis", "units", "values", "psd_tolerance"}, name)
    return Covariance(
        tuple(
            _string(entry, f"{name}.basis")
            for entry in _array(item["basis"], f"{name}.basis")
        ),
        tuple(
            _string(entry, f"{name}.units")
            for entry in _array(item["units"], f"{name}.units")
        ),
        tuple(
            tuple(
                _number(cell, f"{name}.values")
                for cell in _array(row, f"{name}.values")
            )
            for row in _array(item["values"], f"{name}.values")
        ),
        _number(item["psd_tolerance"], f"{name}.psd_tolerance"),
    )


def _provenance(value: object) -> Provenance:
    item = _object(value, "provenance")
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
        "provenance",
    )
    return Provenance(
        _string(item["producer_name"], "producer_name"),
        _string(item["producer_version"], "producer_version"),
        _string(item["git_commit"], "git_commit"),
        _digest(item["environment_digest"], "environment_digest"),
        _digest(item["normalized_config_digest"], "normalized_config_digest"),
        tuple(
            _digest(entry, "input_digest")
            for entry in _array(item["input_digests"], "input_digests")
        ),
        tuple(
            _digest(entry, "dependency_digest")
            for entry in _array(item["dependency_digests"], "dependency_digests")
        ),
        UtcNs(_integer(item["started_utc_ns"], "started_utc_ns")),
        UtcNs(_integer(item["completed_utc_ns"], "completed_utc_ns")),
        _string(item["host_class"], "host_class"),
    )


def _artifact(value: object, name: str) -> ArtifactRef:
    item = _object(value, name)
    _keys(item, {"artifact_id", "digest", "schema"}, name)
    return ArtifactRef(
        _string(item["artifact_id"], f"{name}.artifact_id"),
        _digest(item["digest"], f"{name}.digest"),
        None if item["schema"] is None else _schema(item["schema"], f"{name}.schema"),
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


def _float_pair(value: object, name: str) -> tuple[float, float]:
    items = _array(value, name)
    if len(items) != 2:
        _bad(f"{name} must have two entries")
    return _number(items[0], f"{name}[0]"), _number(items[1], f"{name}[1]")


def _string_pair(value: object, name: str) -> tuple[str, str]:
    items = _array(value, name)
    if len(items) != 2:
        _bad(f"{name} must have two entries")
    return _string(items[0], f"{name}[0]"), _string(items[1], f"{name}[1]")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _bad(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _keys(item: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(item) != expected:
        _bad(f"{name} fields differ from schema")


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
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
    result = float(value)
    if not math.isfinite(result):
        _bad(f"{name} must be finite")
    return result


def _bad(message: str) -> NoReturn:
    raise MalformedTrackingInputError(message)
