"""Read-only dashboard projection for detector evaluation summaries."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from leo_flow.contracts.core import (
    DetectorEvaluationId,
    Digest,
    DigestAlgorithm,
    EvaluationRunId,
)
from leo_flow.contracts.evaluation import (
    DetectorEvaluationRef,
    DetectorEvaluationView,
    DetectorMethodSplitSummary,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.dashboard import DashboardNotFound

from .evaluation_postgres_sql import GET_METHODS_SQL, REPORT_SELECT

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]

_DASHBOARD_REPORT_SQL = (
    REPORT_SELECT
    + """
WHERE er.evaluation_id = %(identity)s OR er.run_id = %(identity)s
"""
)


class PostgresEvaluationDashboard:
    """Expose compact metrics and an exact artifact reference, never filesystem paths."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def detector_evaluation(
        self, evaluation_id_or_run_id: str
    ) -> DetectorEvaluationView:
        if not evaluation_id_or_run_id:
            raise ValueError("evaluation ID or run ID cannot be empty")
        with self._connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            state = connection.execute("SHOW transaction_read_only").fetchone()
            if state is None or state["transaction_read_only"] != "on":
                raise RuntimeError("dashboard transaction is not read-only")
            row = connection.execute(
                _DASHBOARD_REPORT_SQL, {"identity": evaluation_id_or_run_id}
            ).fetchone()
            if row is None:
                raise DashboardNotFound(
                    f"detector evaluation {evaluation_id_or_run_id} was not found"
                )
            method_rows = connection.execute(
                GET_METHODS_SQL, {"evaluation_id": row["evaluation_id"]}
            ).fetchall()
        method_count = _int(row["method_count"], "method_count")
        split_sets: dict[str, set[str]] = {}
        for item in method_rows:
            split_sets.setdefault(str(item["method_id"]), set()).add(str(item["split"]))
        if (
            len(method_rows) != method_count * 3
            or len(split_sets) != method_count
            or any(
                splits != {"train", "validation", "locked_test"}
                for splits in split_sets.values()
            )
        ):
            raise RuntimeError("detector evaluation method projection is incomplete")
        warnings = row["warnings"]
        if not isinstance(warnings, list) or not all(
            isinstance(item, str) for item in warnings
        ):
            raise TypeError("database evaluation warnings are invalid")
        report_ref = ObjectRef(
            _digest(row, "report"),
            _int(row["report_byte_count"], "report_byte_count"),
            str(row["report_media_type"]),
            str(row["report_format_id"]),
            str(row["report_locator"]),
        )
        ref = DetectorEvaluationRef(
            DetectorEvaluationId(str(row["evaluation_id"])),
            EvaluationRunId(str(row["run_id"])),
            report_ref.digest,
            report_ref,
        )
        methods = tuple(
            DetectorMethodSplitSummary(
                str(item["method_id"]),
                str(item["split"]),
                _float(item["threshold"], "threshold"),
                None
                if item["score_semantics"] is None
                else str(item["score_semantics"]),
                *(
                    _int(item[field], field)
                    for field in (
                        "feature_set_count",
                        "feature_set_present_count",
                        "union_window_count",
                        "present_window_count",
                        "missing_window_count",
                        "firing_count",
                        "true_positive",
                        "false_positive",
                        "true_negative",
                        "false_negative",
                        "scored_prediction_count",
                        "missing_prediction_count",
                    )
                ),
            )
            for item in method_rows
        )
        return DetectorEvaluationView(
            ref,
            str(row["dataset_snapshot_id"]),
            _digest(row, "dataset_snapshot"),
            _digest(row, "feature_membership"),
            str(row["threshold_rule_id"]),
            _digest(row, "threshold_rule"),
            str(row["calibration_dataset_id"]),
            str(row["calibration_split"]),
            method_count,
            _int(row["union_window_count"], "union_window_count"),
            tuple(warnings),
            methods,
        )


def connection_factory(dsn: str) -> ConnectionFactory:
    return lambda: psycopg.connect(dsn, row_factory=dict_row)


def _digest(row: dict[str, object], prefix: str) -> Digest:
    return Digest(
        DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
        str(row[f"{prefix}_digest_value"]),
    )


def _int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"database {name} is not an integer")
    return value


def _float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"database {name} is not numeric")
    return float(value)
