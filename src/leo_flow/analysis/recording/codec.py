"""Canonical, bounded codec for one authoritative FeatureSet bundle."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, NoReturn

from leo_flow.contracts.core import (
    AnalysisRunId,
    Digest,
    DigestAlgorithm,
    FeatureId,
    FeatureSetId,
    Provenance,
    ReceiverChainId,
    ReceiverPairId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    SegmentId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.features import (
    Covariance,
    FeatureObservation,
    FeatureSetBundle,
    MethodScore,
)
from leo_flow.contracts.storage import ObjectRef

MAX_FEATURE_SET_BYTES = 64 * 1024 * 1024
FEATURE_SET_MEDIA_TYPE = "application/json"
FEATURE_SET_FORMAT_ID = "feature-set-bundle-v0.1"


class MalformedFeatureSetError(ValueError):
    """Feature bytes are oversized, ambiguous, noncanonical, or invalid."""


def encode_feature_set(bundle: FeatureSetBundle) -> bytes:
    return canonical_json_bytes(bundle)


def decode_feature_set(data: bytes) -> FeatureSetBundle:
    if len(data) > MAX_FEATURE_SET_BYTES:
        raise MalformedFeatureSetError("feature set exceeds size limit")
    try:
        document = json.loads(data, object_pairs_hook=_unique_object)
        if canonical_json_bytes(document) != data:
            _bad("feature set bytes are not canonical JSON")
        root = _object(document, "root")
        _keys(
            root,
            {
                "schema",
                "feature_set_id",
                "analysis_run_id",
                "recording_id",
                "input_recording_identity_digest",
                "provenance",
                "observations",
                "method_scores",
                "diagnostic_bundle_ref",
                "warnings",
                "reason_codes",
            },
            "root",
        )
        schema = _schema(root["schema"], "schema")
        if schema != SchemaRef(FeatureSetBundle.SCHEMA_ID):
            _bad("unsupported durable feature set schema")
        return FeatureSetBundle(
            schema=schema,
            feature_set_id=FeatureSetId(
                _string(root["feature_set_id"], "feature_set_id")
            ),
            analysis_run_id=AnalysisRunId(
                _string(root["analysis_run_id"], "analysis_run_id")
            ),
            recording_id=RecordingId(_string(root["recording_id"], "recording_id")),
            input_recording_identity_digest=_digest(
                root["input_recording_identity_digest"], "input recording digest"
            ),
            provenance=_provenance(root["provenance"]),
            observations=tuple(
                _observation(value, index)
                for index, value in enumerate(
                    _array(root["observations"], "observations")
                )
            ),
            method_scores=tuple(
                _method_score(value, index)
                for index, value in enumerate(
                    _array(root["method_scores"], "method_scores")
                )
            ),
            diagnostic_bundle_ref=(
                None
                if root["diagnostic_bundle_ref"] is None
                else _object_ref(root["diagnostic_bundle_ref"], "diagnostic_bundle_ref")
            ),
            warnings=tuple(
                _string(value, "warning")
                for value in _array(root["warnings"], "warnings")
            ),
            reason_codes=tuple(
                _string(value, "reason code")
                for value in _array(root["reason_codes"], "reason_codes")
            ),
        )
    except MalformedFeatureSetError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedFeatureSetError(str(error)) from error


def _observation(value: object, index: int) -> FeatureObservation:
    name = f"observations[{index}]"
    item = _object(value, name)
    _keys(
        item,
        {
            "feature_id",
            "recording_id",
            "segment_id",
            "method_id",
            "method_version",
            "window_start_sample",
            "window_stop_sample",
            "segment_sample_count",
            "midpoint_utc_ns",
            "feature_kind",
            "score",
            "score_semantics",
            "receiver_chain_id",
            "receiver_pair_id",
            "frequency_hz",
            "frequency_offset_hz",
            "drift_hz_s",
            "noise_estimate",
            "snr_db",
            "covariance",
            "uncertainty",
            "quality_flags",
            "diagnostics",
        },
        name,
    )
    return FeatureObservation(
        feature_id=FeatureId(_string(item["feature_id"], f"{name}.feature_id")),
        recording_id=RecordingId(_string(item["recording_id"], f"{name}.recording_id")),
        segment_id=SegmentId(_string(item["segment_id"], f"{name}.segment_id")),
        method_id=_string(item["method_id"], f"{name}.method_id"),
        method_version=_string(item["method_version"], f"{name}.method_version"),
        window_start_sample=_integer(
            item["window_start_sample"], f"{name}.window_start_sample"
        ),
        window_stop_sample=_integer(
            item["window_stop_sample"], f"{name}.window_stop_sample"
        ),
        segment_sample_count=_integer(
            item["segment_sample_count"], f"{name}.segment_sample_count"
        ),
        midpoint_utc_ns=UtcNs(
            _integer(item["midpoint_utc_ns"], f"{name}.midpoint_utc_ns")
        ),
        feature_kind=_string(item["feature_kind"], f"{name}.feature_kind"),
        score=_number(item["score"], f"{name}.score"),
        score_semantics=_string(item["score_semantics"], f"{name}.score_semantics"),
        receiver_chain_id=(
            None
            if item["receiver_chain_id"] is None
            else ReceiverChainId(
                _string(item["receiver_chain_id"], f"{name}.receiver_chain_id")
            )
        ),
        receiver_pair_id=(
            None
            if item["receiver_pair_id"] is None
            else ReceiverPairId(
                _string(item["receiver_pair_id"], f"{name}.receiver_pair_id")
            )
        ),
        frequency_hz=_optional_number(item["frequency_hz"], f"{name}.frequency_hz"),
        frequency_offset_hz=_optional_number(
            item["frequency_offset_hz"], f"{name}.frequency_offset_hz"
        ),
        drift_hz_s=_optional_number(item["drift_hz_s"], f"{name}.drift_hz_s"),
        noise_estimate=_optional_number(
            item["noise_estimate"], f"{name}.noise_estimate"
        ),
        snr_db=_optional_number(item["snr_db"], f"{name}.snr_db"),
        covariance=(
            None
            if item["covariance"] is None
            else _covariance(item["covariance"], f"{name}.covariance")
        ),
        uncertainty=_pairs(item["uncertainty"], f"{name}.uncertainty"),
        quality_flags=tuple(
            _string(entry, f"{name}.quality_flags")
            for entry in _array(item["quality_flags"], f"{name}.quality_flags")
        ),
        diagnostics=_pairs(item["diagnostics"], f"{name}.diagnostics"),
    )


def _method_score(value: object, index: int) -> MethodScore:
    name = f"method_scores[{index}]"
    item = _object(value, name)
    _keys(
        item,
        {
            "method_id",
            "method_version",
            "segment_id",
            "receiver_key",
            "window_start_sample",
            "window_stop_sample",
            "score",
            "score_semantics",
        },
        name,
    )
    return MethodScore(
        method_id=_string(item["method_id"], f"{name}.method_id"),
        method_version=_string(item["method_version"], f"{name}.method_version"),
        segment_id=SegmentId(_string(item["segment_id"], f"{name}.segment_id")),
        receiver_key=_string(item["receiver_key"], f"{name}.receiver_key"),
        window_start_sample=_integer(
            item["window_start_sample"], "window_start_sample"
        ),
        window_stop_sample=_integer(item["window_stop_sample"], "window_stop_sample"),
        score=_number(item["score"], f"{name}.score"),
        score_semantics=_string(item["score_semantics"], f"{name}.score_semantics"),
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


def _object_ref(value: object, name: str) -> ObjectRef:
    item = _object(value, name)
    _keys(item, {"digest", "byte_count", "media_type", "format_id", "locator"}, name)
    return ObjectRef(
        _digest(item["digest"], f"{name}.digest"),
        _integer(item["byte_count"], f"{name}.byte_count"),
        _string(item["media_type"], f"{name}.media_type"),
        _string(item["format_id"], f"{name}.format_id"),
        _string(item["locator"], f"{name}.locator"),
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


def _pairs(value: object, name: str) -> tuple[tuple[str, Any], ...]:
    result: list[tuple[str, Any]] = []
    for index, pair in enumerate(_array(value, name)):
        cells = _array(pair, f"{name}[{index}]")
        if len(cells) != 2:
            _bad(f"{name}[{index}] must contain two values")
        result.append((_string(cells[0], f"{name}[{index}].key"), _freeze(cells[1])))
    return tuple(result)


def _freeze(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, dict):
        return tuple((key, _freeze(item)) for key, item in sorted(value.items()))
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _bad(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _object(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _bad(f"{name} must be an object")
    return value


def _array(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        _bad(f"{name} must be an array")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        _bad(f"{name} fields differ from the schema")


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


def _optional_number(value: object, name: str) -> float | None:
    return None if value is None else _number(value, name)


def _bad(message: str) -> NoReturn:
    raise MalformedFeatureSetError(message)
