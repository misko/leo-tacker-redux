"""Atomic append-only PostgreSQL catalog for detector evaluation reports."""

from __future__ import annotations

import json
from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from leo_flow.analysis.dataset.evaluation import (
    DETECTOR_EVALUATION_FORMAT_ID,
    DETECTOR_EVALUATION_MEDIA_TYPE,
    DetectorEvaluationReport,
)
from leo_flow.analysis.dataset.evaluation_codec import encode_detector_evaluation
from leo_flow.analysis.dataset.evaluation_persistence import (
    CatalogedEvaluation,
    DetectorEvaluationIntegrityError,
    EvaluationCatalogProjection,
    evaluation_projection,
)
from leo_flow.contracts.core import (
    DetectorEvaluationId,
    Digest,
    DigestAlgorithm,
    EvaluationRunId,
)
from leo_flow.contracts.evaluation import (
    DetectorEvaluationRef,
    DetectorMethodSplitSummary,
)
from leo_flow.contracts.storage import ObjectRef

from . import evaluation_postgres_sql as sql

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresEvaluationError(RuntimeError):
    pass


class EvaluationConflictError(PostgresEvaluationError):
    pass


class EvaluationObjectCollisionError(PostgresEvaluationError):
    pass


class EvaluationDatasetMismatchError(PostgresEvaluationError):
    pass


class PostgresEvaluationCatalog:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish(
        self,
        projection: EvaluationCatalogProjection,
        report_object: ObjectRef,
        report: DetectorEvaluationReport,
        *,
        idempotency_key: str,
    ) -> DetectorEvaluationRef:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        if (
            evaluation_projection(EvaluationRunId(projection.run_id), report)
            != projection
            or Digest.sha256(encode_detector_evaluation(report)) != report_object.digest
            or report_object.media_type != DETECTOR_EVALUATION_MEDIA_TYPE
            or report_object.format_id != DETECTOR_EVALUATION_FORMAT_ID
        ):
            raise DetectorEvaluationIntegrityError(
                "catalog projection or object digest differs from report"
            )
        parameters = _parameters(projection, report_object, idempotency_key)
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(sql.VERIFY_DATASET_SQL, parameters)
            if cursor.fetchone() is None:
                raise EvaluationDatasetMismatchError(
                    "evaluation does not reference the exact authoritative dataset"
                )
            cursor.execute(sql.REGISTER_OBJECT_SQL, parameters)
            cursor.execute(sql.VERIFY_OBJECT_SQL, parameters)
            row = cursor.fetchone()
            if row is None or (
                _integer(row["byte_count"], "byte_count") != report_object.byte_count
                or row["media_type"] != report_object.media_type
                or row["format_id"] != report_object.format_id
                or row["locator"] != report_object.locator
            ):
                raise EvaluationObjectCollisionError("report object metadata conflicts")
            cursor.execute(sql.PUBLISH_REPORT_SQL, parameters)
            inserted = cursor.fetchone() is not None
            if inserted:
                for method in report.methods:
                    for split in method.by_split:
                        cursor.execute(
                            sql.PUBLISH_METHOD_SQL,
                            {
                                **parameters,
                                "method_id": method.method_id,
                                "split": split.split,
                                "threshold": method.threshold,
                                "score_semantics": method.score_semantics,
                                "feature_set_count": split.feature_set_count,
                                "feature_set_present_count": split.feature_set_present_count,
                                "split_union_window_count": split.union_window_count,
                                "present_window_count": split.present_window_count,
                                "missing_window_count": split.missing_window_count,
                                "firing_count": split.firing_count,
                                "true_positive": split.truth.true_positive,
                                "false_positive": split.truth.false_positive,
                                "true_negative": split.truth.true_negative,
                                "false_negative": split.truth.false_negative,
                                "scored_prediction_count": split.truth.scored_prediction_count,
                                "missing_prediction_count": split.truth.missing_prediction_count,
                            },
                        )
                return _ref(projection, report_object)
            cursor.execute(sql.GET_CONFLICTS_SQL, parameters)
            rows = cursor.fetchall()
            if len(rows) != 1:
                raise EvaluationConflictError(
                    "evaluation identities identify different rows"
                )
            existing = _cataloged(rows[0])
            if str(
                rows[0]["idempotency_key"]
            ) != idempotency_key or existing != CatalogedEvaluation(
                projection, report_object
            ):
                raise EvaluationConflictError(
                    "evaluation identity or idempotency key was reused differently"
                )
            cursor.execute(sql.GET_METHODS_SQL, parameters)
            if _database_method_summaries(
                cursor.fetchall()
            ) != _report_method_summaries(report):
                raise EvaluationConflictError(
                    "published evaluation method summaries differ from report"
                )
            return existing.ref

    def get(self, ref: DetectorEvaluationRef) -> CatalogedEvaluation | None:
        parameters = {
            "evaluation_id": str(ref.evaluation_id),
            "run_id": str(ref.run_id),
            "report_digest_algorithm": ref.report_digest.algorithm.value,
            "report_digest_value": ref.report_digest.value,
        }
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(sql.GET_EXACT_SQL, parameters)
            row = cursor.fetchone()
            if row is None:
                return None
            result = _cataloged(row)
            return result if result.report_object == ref.report_object else None


def connection_factory(dsn: str) -> ConnectionFactory:
    return lambda: psycopg.connect(dsn, row_factory=dict_row)


def _parameters(
    projection: EvaluationCatalogProjection, ref: ObjectRef, key: str
) -> dict[str, object]:
    return {
        "evaluation_id": projection.evaluation_id,
        "run_id": projection.run_id,
        "dataset_snapshot_id": projection.dataset_snapshot_id,
        "dataset_snapshot_digest_algorithm": projection.dataset_snapshot_digest.algorithm.value,
        "dataset_snapshot_digest_value": projection.dataset_snapshot_digest.value,
        "membership_digest_algorithm": projection.feature_membership_digest.algorithm.value,
        "membership_digest_value": projection.feature_membership_digest.value,
        "threshold_rule_id": projection.threshold_rule_id,
        "threshold_rule_digest_algorithm": projection.threshold_rule_digest.algorithm.value,
        "threshold_rule_digest_value": projection.threshold_rule_digest.value,
        "calibration_dataset_id": projection.calibration_dataset_id,
        "calibration_split": projection.calibration_split,
        "report_digest_algorithm": ref.digest.algorithm.value,
        "report_digest_value": ref.digest.value,
        "report_byte_count": ref.byte_count,
        "report_media_type": ref.media_type,
        "report_format_id": ref.format_id,
        "report_locator": ref.locator,
        "method_count": projection.method_count,
        "union_window_count": projection.union_window_count,
        "warnings": json.dumps(projection.warnings, separators=(",", ":")),
        "idempotency_key": key,
    }


def _cataloged(row: dict[str, object]) -> CatalogedEvaluation:
    warnings = row["warnings"]
    if not isinstance(warnings, list) or not all(
        isinstance(item, str) for item in warnings
    ):
        raise PostgresEvaluationError("database warnings are invalid")
    projection = EvaluationCatalogProjection(
        str(row["evaluation_id"]),
        str(row["run_id"]),
        str(row["dataset_snapshot_id"]),
        _digest(row, "dataset_snapshot"),
        _digest(row, "feature_membership"),
        str(row["threshold_rule_id"]),
        _digest(row, "threshold_rule"),
        str(row["calibration_dataset_id"]),
        str(row["calibration_split"]),
        _integer(row["method_count"], "method_count"),
        _integer(row["union_window_count"], "union_window_count"),
        tuple(warnings),
    )
    ref = ObjectRef(
        _digest(row, "report"),
        _integer(row["report_byte_count"], "report_byte_count"),
        str(row["report_media_type"]),
        str(row["report_format_id"]),
        str(row["report_locator"]),
    )
    return CatalogedEvaluation(projection, ref)


def _ref(
    projection: EvaluationCatalogProjection, ref: ObjectRef
) -> DetectorEvaluationRef:
    return DetectorEvaluationRef(
        DetectorEvaluationId(projection.evaluation_id),
        EvaluationRunId(projection.run_id),
        ref.digest,
        ref,
    )


def _digest(row: dict[str, object], prefix: str) -> Digest:
    return Digest(
        DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
        str(row[f"{prefix}_digest_value"]),
    )


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PostgresEvaluationError(f"database {name} is not an integer")
    return value


def _report_method_summaries(
    report: DetectorEvaluationReport,
) -> tuple[DetectorMethodSplitSummary, ...]:
    rows = (
        DetectorMethodSplitSummary(
            method.method_id,
            split.split,
            method.threshold,
            method.score_semantics,
            split.feature_set_count,
            split.feature_set_present_count,
            split.union_window_count,
            split.present_window_count,
            split.missing_window_count,
            split.firing_count,
            split.truth.true_positive,
            split.truth.false_positive,
            split.truth.true_negative,
            split.truth.false_negative,
            split.truth.scored_prediction_count,
            split.truth.missing_prediction_count,
        )
        for method in report.methods
        for split in method.by_split
    )
    split_order = {"train": 0, "validation": 1, "locked_test": 2}
    return tuple(
        sorted(rows, key=lambda item: (item.method_id, split_order[item.split]))
    )


def _database_method_summaries(
    rows: list[dict[str, object]],
) -> tuple[DetectorMethodSplitSummary, ...]:
    return tuple(_database_method_summary(row) for row in rows)


def _database_method_summary(row: dict[str, object]) -> DetectorMethodSplitSummary:
    return DetectorMethodSplitSummary(
        str(row["method_id"]),
        str(row["split"]),
        _number(row["threshold"], "threshold"),
        None if row["score_semantics"] is None else str(row["score_semantics"]),
        _integer(row["feature_set_count"], "feature_set_count"),
        _integer(row["feature_set_present_count"], "feature_set_present_count"),
        _integer(row["union_window_count"], "union_window_count"),
        _integer(row["present_window_count"], "present_window_count"),
        _integer(row["missing_window_count"], "missing_window_count"),
        _integer(row["firing_count"], "firing_count"),
        _integer(row["true_positive"], "true_positive"),
        _integer(row["false_positive"], "false_positive"),
        _integer(row["true_negative"], "true_negative"),
        _integer(row["false_negative"], "false_negative"),
        _integer(row["scored_prediction_count"], "scored_prediction_count"),
        _integer(row["missing_prediction_count"], "missing_prediction_count"),
    )


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PostgresEvaluationError(f"database {name} is not numeric")
    return float(value)
