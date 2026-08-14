"""Strict, bounded codec for the canonical detector evaluation report."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import NoReturn

from leo_flow.contracts.core import (
    Digest,
    DigestAlgorithm,
    SchemaRef,
    SchemaVersion,
    canonical_json_bytes,
)

from .association import MethodAssociationReport
from .evaluation import (
    BinaryClassificationCounts,
    DetectorEvaluationReport,
    MethodEvaluation,
    SplitAssociationReport,
    SplitMethodReport,
)

MAX_DETECTOR_EVALUATION_BYTES = 16 * 1024 * 1024


class MalformedDetectorEvaluationError(ValueError):
    pass


def encode_detector_evaluation(report: DetectorEvaluationReport) -> bytes:
    payload = canonical_json_bytes(report)
    if len(payload) > MAX_DETECTOR_EVALUATION_BYTES:
        raise MalformedDetectorEvaluationError("detector evaluation exceeds size limit")
    return payload


def decode_detector_evaluation(data: bytes) -> DetectorEvaluationReport:
    if len(data) > MAX_DETECTOR_EVALUATION_BYTES:
        raise MalformedDetectorEvaluationError("detector evaluation exceeds size limit")
    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
        if canonical_json_bytes(value) != data:
            _bad("detector evaluation bytes are not canonical JSON")
        root = _object(value, "root")
        _keys(
            root,
            {
                "schema",
                "dataset_snapshot_id",
                "dataset_snapshot_digest",
                "feature_membership_digest",
                "threshold_rule_id",
                "threshold_rule_digest",
                "threshold_calibration_dataset_id",
                "threshold_calibration_split",
                "methods",
                "overall_association",
                "association_by_split",
                "warnings",
            },
            "root",
        )
        report = DetectorEvaluationReport(
            _schema(root["schema"]),
            _string(root["dataset_snapshot_id"], "dataset_snapshot_id"),
            _digest(root["dataset_snapshot_digest"], "dataset_snapshot_digest"),
            _digest(root["feature_membership_digest"], "feature_membership_digest"),
            _string(root["threshold_rule_id"], "threshold_rule_id"),
            _digest(root["threshold_rule_digest"], "threshold_rule_digest"),
            _string(
                root["threshold_calibration_dataset_id"],
                "threshold_calibration_dataset_id",
            ),
            _string(root["threshold_calibration_split"], "threshold_calibration_split"),
            tuple(
                _method(item, index)
                for index, item in enumerate(_array(root["methods"], "methods"))
            ),
            _association(root["overall_association"], "overall_association"),
            tuple(
                _split_association(item, index)
                for index, item in enumerate(
                    _array(root["association_by_split"], "association_by_split")
                )
            ),
            tuple(
                _string(item, "warning")
                for item in _array(root["warnings"], "warnings")
            ),
        )
        if report.canonical_bytes() != data:
            _bad("decoded report does not reproduce canonical bytes")
        return report
    except MalformedDetectorEvaluationError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedDetectorEvaluationError(str(error)) from error


def _method(value: object, index: int) -> MethodEvaluation:
    name = f"methods[{index}]"
    item = _object(value, name)
    _keys(item, {"method_id", "threshold", "score_semantics", "by_split"}, name)
    semantics = item["score_semantics"]
    if semantics is not None:
        semantics = _string(semantics, f"{name}.score_semantics")
    return MethodEvaluation(
        _string(item["method_id"], f"{name}.method_id"),
        _number(item["threshold"], f"{name}.threshold"),
        semantics,
        tuple(
            _split_method(row, index, split_index)
            for split_index, row in enumerate(
                _array(item["by_split"], f"{name}.by_split")
            )
        ),
    )


def _split_method(
    value: object, method_index: int, split_index: int
) -> SplitMethodReport:
    name = f"methods[{method_index}].by_split[{split_index}]"
    item = _object(value, name)
    fields = {
        "split",
        "feature_set_count",
        "feature_set_present_count",
        "union_window_count",
        "present_window_count",
        "missing_window_count",
        "firing_count",
        "truth",
    }
    _keys(item, fields, name)
    truth = _object(item["truth"], f"{name}.truth")
    truth_fields = {
        "true_positive",
        "false_positive",
        "true_negative",
        "false_negative",
        "admissible_truth_count",
        "scored_prediction_count",
        "missing_prediction_count",
        "inadmissible_truth_count",
        "context_only_count",
    }
    _keys(truth, truth_fields, f"{name}.truth")
    return SplitMethodReport(
        _string(item["split"], f"{name}.split"),
        _integer(item["feature_set_count"], f"{name}.feature_set_count"),
        _integer(
            item["feature_set_present_count"],
            f"{name}.feature_set_present_count",
        ),
        _integer(item["union_window_count"], f"{name}.union_window_count"),
        _integer(item["present_window_count"], f"{name}.present_window_count"),
        _integer(item["missing_window_count"], f"{name}.missing_window_count"),
        _integer(item["firing_count"], f"{name}.firing_count"),
        BinaryClassificationCounts(
            *(
                _integer(truth[field], f"{name}.truth.{field}")
                for field in (
                    "true_positive",
                    "false_positive",
                    "true_negative",
                    "false_negative",
                    "admissible_truth_count",
                    "scored_prediction_count",
                    "missing_prediction_count",
                    "inadmissible_truth_count",
                    "context_only_count",
                )
            )
        ),
    )


def _split_association(value: object, index: int) -> SplitAssociationReport:
    name = f"association_by_split[{index}]"
    item = _object(value, name)
    _keys(item, {"split", "association"}, name)
    return SplitAssociationReport(
        _string(item["split"], f"{name}.split"),
        _association(item["association"], f"{name}.association"),
    )


def _association(value: object, name: str) -> MethodAssociationReport:
    item = _object(value, name)
    fields = {
        "method_ids",
        "firing_covariance",
        "phi",
        "shared_window_count",
        "shared_sample_count",
        "method_present_window_count",
        "union_window_count",
        "missing_window_count",
    }
    _keys(item, fields, name)
    methods = tuple(
        _string(v, f"{name}.method_id")
        for v in _array(item["method_ids"], f"{name}.method_ids")
    )
    n = len(methods)

    def matrix(field: str, optional: bool) -> tuple[tuple[float | None, ...], ...]:
        rows = _array(item[field], f"{name}.{field}")
        if len(rows) != n:
            _bad(f"{name}.{field} has wrong dimension")
        result = []
        for row in rows:
            values = _array(row, f"{name}.{field}")
            if len(values) != n:
                _bad(f"{name}.{field} has wrong dimension")
            result.append(
                tuple(
                    None
                    if optional and cell is None
                    else _number(cell, f"{name}.{field}")
                    for cell in values
                )
            )
        return tuple(result)

    def int_matrix(field: str) -> tuple[tuple[int, ...], ...]:
        rows = _array(item[field], f"{name}.{field}")
        if len(rows) != n:
            _bad(f"{name}.{field} has wrong dimension")
        result = []
        for row in rows:
            values = _array(row, f"{name}.{field}")
            if len(values) != n:
                _bad(f"{name}.{field} has wrong dimension")
            result.append(tuple(_integer(cell, f"{name}.{field}") for cell in values))
        return tuple(result)

    present = tuple(
        _integer(v, f"{name}.method_present_window_count")
        for v in _array(
            item["method_present_window_count"], f"{name}.method_present_window_count"
        )
    )
    missing = tuple(
        _integer(v, f"{name}.missing_window_count")
        for v in _array(item["missing_window_count"], f"{name}.missing_window_count")
    )
    if len(present) != n or len(missing) != n:
        _bad(f"{name} method vector has wrong dimension")
    return MethodAssociationReport(
        methods,
        matrix("firing_covariance", True),
        matrix("phi", True),
        int_matrix("shared_window_count"),
        int_matrix("shared_sample_count"),
        present,
        _integer(item["union_window_count"], f"{name}.union_window_count"),
        missing,
    )


def _digest(value: object, name: str) -> Digest:
    item = _object(value, name)
    _keys(item, {"algorithm", "value"}, name)
    return Digest(
        DigestAlgorithm(_string(item["algorithm"], f"{name}.algorithm")),
        _string(item["value"], f"{name}.value"),
    )


def _schema(value: object) -> SchemaRef:
    item = _object(value, "schema")
    _keys(item, {"schema_id", "version"}, "schema")
    version = _object(item["version"], "schema.version")
    _keys(version, {"major", "minor"}, "schema.version")
    return SchemaRef(
        _string(item["schema_id"], "schema.schema_id"),
        SchemaVersion(
            _integer(version["major"], "schema.version.major"),
            _integer(version["minor"], "schema.version.minor"),
        ),
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


def _keys(value: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        _bad(f"{name} fields differ")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        _bad(f"{name} must be a non-empty string")
    return value


def _number(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        _bad(f"{name} must be a finite number")
    return float(value)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _bad(f"{name} must be a non-negative integer")
    return value


def _bad(message: str) -> NoReturn:
    raise MalformedDetectorEvaluationError(message)
