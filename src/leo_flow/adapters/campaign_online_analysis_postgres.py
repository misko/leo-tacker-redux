"""PostgreSQL ports for capture-safe campaign-local online analysis."""

from __future__ import annotations

from collections.abc import Callable

import psycopg

from leo_flow.contracts.core import Digest
from leo_flow.contracts.deferred_analysis import DeferredAnalysisWindowV1

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresCampaignAnalysisScopeRegistrarV1:
    """Register one validated terminal window before any worker can lease it."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def register(self, window: DeferredAnalysisWindowV1) -> None:
        parameters = (
            str(window.definition_digest),
            str(window.identity_digest),
            window.first_success_index,
            [str(value) for value in window.batch_ids],
            [str(value) for value in window.recording_ids],
            [str(value) for value in window.recording_identity_digests],
            [str(value) for value in window.feature_job_ids],
            [str(value) for value in window.waterfall_job_ids],
            [str(value) for value in window.starlink_suite_job_ids],
        )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT public.register_campaign_analysis_window_scope_v1("
                "%s,%s,%s,%s,%s,%s,%s,%s,%s) AS registered",
                parameters,
            ).fetchone()
        if row is None or row["registered"] is not True:
            raise RuntimeError("campaign analysis scope registration failed")


class PostgresCampaignConcurrentAnalysisGateV1:
    """Permit only this campaign's registered terminal work during capture."""

    def __init__(self, dsn: str, definition_digest: Digest) -> None:
        if not dsn:
            raise ValueError("catalog DSN cannot be empty")
        self._dsn = dsn
        self._definition_digest = definition_digest

    def ready(self) -> bool:
        with psycopg.connect(
            self._dsn,
            connect_timeout=5,
            options="-c statement_timeout=5000 -c lock_timeout=5000",
        ) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            membership = connection.execute(
                "SELECT pg_has_role(current_user,%s,'MEMBER')", ("leo_capture",)
            ).fetchone()
            if membership is None or membership[0] is not True:
                raise RuntimeError(
                    "catalog credential is not a leo_capture role member"
                )
            connection.execute("SET ROLE leo_capture")
            row = connection.execute(
                "SELECT public.capture_campaign_analysis_safe_v1(%s) AS ready",
                (str(self._definition_digest),),
            ).fetchone()
        if row is None or not isinstance(row[0], bool):
            raise RuntimeError("campaign analysis safety gate returned no decision")
        return row[0]
