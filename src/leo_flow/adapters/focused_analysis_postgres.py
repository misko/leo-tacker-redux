"""PostgreSQL registration and capture gate for focused pair analysis."""

from __future__ import annotations

from collections.abc import Callable

import psycopg

from leo_flow.contracts.core import Digest
from leo_flow.contracts.focused_analysis import FocusedAnalysisPairScopeV0_1

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresFocusedAnalysisPairScopeRegistrarV0_1:
    """Register a terminal pair before any of its jobs may be leased."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def register(self, scope: FocusedAnalysisPairScopeV0_1) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT public.register_focused_analysis_pair_scope_v1("
                "%s,%s,%s,%s,%s,%s,%s,%s) AS registered",
                (
                    str(scope.capture_definition_digest),
                    str(scope.identity_digest),
                    str(scope.batch_id),
                    [str(value) for value in scope.recording_ids],
                    [str(value) for value in scope.recording_identity_digests],
                    [str(value) for value in scope.feature_job_ids],
                    [str(value) for value in scope.waterfall_job_ids],
                    [str(value) for value in scope.starlink_suite_job_ids],
                ),
            ).fetchone()
        if row is None or row["registered"] is not True:
            raise RuntimeError("focused analysis pair scope registration failed")


class PostgresRegisteredAnalysisSafetyGateV3:
    """Permit only registered window or focused-pair work during capture."""

    def __init__(self, dsn: str, capture_definition_digest: Digest) -> None:
        if not dsn:
            raise ValueError("catalog DSN cannot be empty")
        self._dsn = dsn
        self._capture_definition_digest = capture_definition_digest

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
                "SELECT public.capture_registered_analysis_safe_v3(%s) AS ready",
                (str(self._capture_definition_digest),),
            ).fetchone()
        if row is None or not isinstance(row[0], bool):
            raise RuntimeError("registered analysis safety gate returned no decision")
        return row[0]
