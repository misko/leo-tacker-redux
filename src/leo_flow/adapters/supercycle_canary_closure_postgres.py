"""Exact PostgreSQL closure proof for one 72-recording canary window."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from leo_flow.contracts.deferred_analysis import (
    DEFERRED_ANALYSIS_RECORDINGS,
    DeferredAnalysisWindowV1,
)

ConnectionFactory = Callable[[], Any]


class PostgresSupercycleCanaryClosureReaderV1:
    def __init__(
        self,
        analysis_connect: ConnectionFactory,
        dashboard_connect: ConnectionFactory,
    ) -> None:
        self._analysis_connect = analysis_connect
        self._dashboard_connect = dashboard_connect

    def counts(
        self, definition_digest: str, window: DeferredAnalysisWindowV1
    ) -> tuple[int, int, int, int]:
        if definition_digest != str(window.definition_digest):
            raise ValueError("canary closure definition digest differs")
        recording_ids = _exact_strings(window.recording_ids, "recording")
        feature_ids = _exact_strings(window.feature_job_ids, "feature job")
        waterfall_ids = _exact_strings(window.waterfall_job_ids, "waterfall job")
        suite_ids = _exact_strings(window.starlink_suite_job_ids, "suite job")
        with self._analysis_connect() as connection:
            feature_count = _receipt_count(
                connection,
                "read_feature_projection_receipt",
                feature_ids,
                recording_ids,
                "r.work_state='succeeded' AND r.job_state='succeeded' "
                "AND r.projected_at_utc IS NOT NULL",
            )
            waterfall_count = _receipt_count(
                connection,
                "read_waterfall_analysis_receipt",
                waterfall_ids,
                recording_ids,
                "r.work_state='succeeded' AND r.job_state='succeeded' "
                "AND r.projected_at_utc IS NOT NULL",
            )
            suite_rows = connection.execute(
                """
                SELECT r.recording_id,r.analysis_id
                  FROM pg_catalog.unnest(%s::text[]) AS requested(source_job_id)
                  CROSS JOIN LATERAL
                    public.read_starlink_detector_suite_receipt(
                      requested.source_job_id) AS r
                 WHERE r.recording_id=ANY(%s::text[])
                   AND r.work_state='succeeded' AND r.job_state='succeeded'
                   AND r.projected_at_utc IS NOT NULL
                   AND r.result_state IN ('candidates','not_evaluated')
                """,
                (suite_ids, recording_ids),
            ).fetchall()
        suite_pairs = tuple(
            (str(row["recording_id"]), str(row["analysis_id"])) for row in suite_rows
        )
        if (
            len(suite_pairs) != DEFERRED_ANALYSIS_RECORDINGS
            or len(set(suite_pairs)) != DEFERRED_ANALYSIS_RECORDINGS
            or {item[0] for item in suite_pairs} != set(recording_ids)
        ):
            suite_count = len(suite_pairs)
            return feature_count, waterfall_count, suite_count, 0
        with self._dashboard_connect() as connection:
            row = connection.execute(
                """
                WITH expected(recording_id,analysis_id) AS (
                    SELECT * FROM pg_catalog.unnest(%s::text[],%s::text[])
                )
                SELECT pg_catalog.count(DISTINCT
                           (projection.recording_id,projection.analysis_id)) AS count
                  FROM expected
                  JOIN public.dashboard_recording_starlink_detector_suite_projection
                    AS projection USING(recording_id,analysis_id)
                 WHERE projection.semantic_view->>'state'
                       IN ('candidates','not_evaluated')
                """,
                (
                    [item[0] for item in suite_pairs],
                    [item[1] for item in suite_pairs],
                ),
            ).fetchone()
        dashboard_count = 0 if row is None else int(row["count"])
        return (
            feature_count,
            waterfall_count,
            len(suite_pairs),
            dashboard_count,
        )


def _exact_strings(values: Sequence[object], label: str) -> list[str]:
    result = [str(value) for value in values]
    if (
        len(result) != DEFERRED_ANALYSIS_RECORDINGS
        or len(set(result)) != DEFERRED_ANALYSIS_RECORDINGS
    ):
        raise ValueError(f"canary closure {label} identities differ")
    return result


def _receipt_count(
    connection: Any,
    function: str,
    source_job_ids: list[str],
    recording_ids: list[str],
    closure: str,
) -> int:
    if function not in {
        "read_feature_projection_receipt",
        "read_waterfall_analysis_receipt",
    }:
        raise ValueError("canary closure receipt function differs")
    row = connection.execute(
        f"""
        SELECT pg_catalog.count(*) AS count,
               pg_catalog.count(DISTINCT r.recording_id) AS recording_count
          FROM pg_catalog.unnest(%s::text[]) AS requested(source_job_id)
          CROSS JOIN LATERAL public.{function}(requested.source_job_id) AS r
         WHERE r.recording_id=ANY(%s::text[]) AND {closure}
        """,
        (source_job_ids, recording_ids),
    ).fetchone()
    if row is None:
        return 0
    count = int(row["count"])
    if (
        count != DEFERRED_ANALYSIS_RECORDINGS
        or int(row["recording_count"]) != DEFERRED_ANALYSIS_RECORDINGS
    ):
        return 0
    return count
