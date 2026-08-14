"""Strict bounded canonical codec for an authoritative model snapshot."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import NoReturn

from leo_flow.contracts.core import (
    Digest,
    DigestAlgorithm,
    ModelRunId,
    ModelSnapshotId,
    Provenance,
    SchemaRef,
    SchemaVersion,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.features import Covariance
from leo_flow.contracts.model import ModelSnapshotBundle, ParameterEstimate

MAX_MODEL_SNAPSHOT_BYTES = 64 * 1024 * 1024
MODEL_SNAPSHOT_MEDIA_TYPE = "application/json"
MODEL_SNAPSHOT_FORMAT_ID = "model-snapshot-bundle-v0.1"


class MalformedModelSnapshotError(ValueError):
    pass


def encode_model_snapshot(bundle: ModelSnapshotBundle) -> bytes:
    payload = canonical_json_bytes(bundle)
    if len(payload) > MAX_MODEL_SNAPSHOT_BYTES:
        raise MalformedModelSnapshotError("model snapshot exceeds size limit")
    return payload


def decode_model_snapshot(data: bytes) -> ModelSnapshotBundle:
    if len(data) > MAX_MODEL_SNAPSHOT_BYTES:
        raise MalformedModelSnapshotError("model snapshot exceeds size limit")
    try:
        document = json.loads(data, object_pairs_hook=_unique_object)
        if canonical_json_bytes(document) != data:
            _bad("model snapshot bytes are not canonical JSON")
        root = _object(document, "root")
        _keys(
            root,
            {
                "schema",
                "model_snapshot_id",
                "model_run_id",
                "dataset_membership_digest",
                "hardware_snapshot_digests",
                "ephemeris_snapshot_digests",
                "provenance",
                "parameters",
                "warnings",
            },
            "root",
        )
        schema = _schema(root["schema"], "schema")
        if schema != SchemaRef(ModelSnapshotBundle.SCHEMA_ID):
            _bad("unsupported durable model snapshot schema")
        return ModelSnapshotBundle(
            schema=schema,
            model_snapshot_id=ModelSnapshotId(
                _string(root["model_snapshot_id"], "model_snapshot_id")
            ),
            model_run_id=ModelRunId(_string(root["model_run_id"], "model_run_id")),
            dataset_membership_digest=_digest(
                root["dataset_membership_digest"], "dataset_membership_digest"
            ),
            hardware_snapshot_digests=tuple(
                _digest(item, "hardware_snapshot_digest")
                for item in _array(
                    root["hardware_snapshot_digests"], "hardware_snapshot_digests"
                )
            ),
            ephemeris_snapshot_digests=tuple(
                _digest(item, "ephemeris_snapshot_digest")
                for item in _array(
                    root["ephemeris_snapshot_digests"],
                    "ephemeris_snapshot_digests",
                )
            ),
            provenance=_provenance(root["provenance"]),
            parameters=tuple(
                _parameter(item, index)
                for index, item in enumerate(_array(root["parameters"], "parameters"))
            ),
            warnings=tuple(
                _string(item, "warning")
                for item in _array(root["warnings"], "warnings")
            ),
        )
    except MalformedModelSnapshotError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedModelSnapshotError(str(error)) from error


def _parameter(value: object, index: int) -> ParameterEstimate:
    name = f"parameters[{index}]"
    item = _object(value, name)
    _keys(
        item,
        {"parameter_id", "subject_id", "value", "basis", "units", "covariance"},
        name,
    )
    return ParameterEstimate(
        parameter_id=_string(item["parameter_id"], f"{name}.parameter_id"),
        subject_id=_string(item["subject_id"], f"{name}.subject_id"),
        value=tuple(
            _number(entry, f"{name}.value")
            for entry in _array(item["value"], f"{name}.value")
        ),
        basis=tuple(
            _string(entry, f"{name}.basis")
            for entry in _array(item["basis"], f"{name}.basis")
        ),
        units=tuple(
            _string(entry, f"{name}.units")
            for entry in _array(item["units"], f"{name}.units")
        ),
        covariance=_covariance(item["covariance"], f"{name}.covariance"),
    )


def _covariance(value: object, name: str) -> Covariance:
    item = _object(value, name)
    _keys(item, {"basis", "units", "values", "psd_tolerance"}, name)
    return Covariance(
        basis=tuple(
            _string(entry, f"{name}.basis")
            for entry in _array(item["basis"], f"{name}.basis")
        ),
        units=tuple(
            _string(entry, f"{name}.units")
            for entry in _array(item["units"], f"{name}.units")
        ),
        values=tuple(
            tuple(
                _number(cell, f"{name}.values")
                for cell in _array(row, f"{name}.values")
            )
            for row in _array(item["values"], f"{name}.values")
        ),
        psd_tolerance=_number(item["psd_tolerance"], f"{name}.psd_tolerance"),
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
        producer_name=_string(item["producer_name"], "producer_name"),
        producer_version=_string(item["producer_version"], "producer_version"),
        git_commit=_string(item["git_commit"], "git_commit"),
        environment_digest=_digest(item["environment_digest"], "environment_digest"),
        normalized_config_digest=_digest(
            item["normalized_config_digest"], "normalized_config_digest"
        ),
        input_digests=tuple(
            _digest(entry, "input_digest")
            for entry in _array(item["input_digests"], "input_digests")
        ),
        dependency_digests=tuple(
            _digest(entry, "dependency_digest")
            for entry in _array(item["dependency_digests"], "dependency_digests")
        ),
        started_utc_ns=UtcNs(_integer(item["started_utc_ns"], "started_utc_ns")),
        completed_utc_ns=UtcNs(_integer(item["completed_utc_ns"], "completed_utc_ns")),
        host_class=_string(item["host_class"], "host_class"),
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
            _bad(f"duplicate object key: {key}")
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
    raise MalformedModelSnapshotError(message)
