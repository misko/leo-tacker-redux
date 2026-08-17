"""Narrow PostgreSQL gate between capture and the local analysis pipeline."""

from __future__ import annotations

import psycopg

_POSTGRES_TIMEOUT_S = 5
_CAPTURE_ROLE = "leo_capture"
_MEMBERSHIP_SQL = """
SELECT pg_has_role(current_user, %s, 'MEMBER') AS member
"""
_DRAIN_READY_SQL = """
SELECT public.capture_analysis_drain_ready() AS ready
"""


class PostgresCaptureAnalysisDrainGate:
    """Report whether all prior recording-analysis delivery is terminal."""

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("catalog DSN cannot be empty")
        self._dsn = dsn

    def ready(self) -> bool:
        with psycopg.connect(
            self._dsn,
            connect_timeout=_POSTGRES_TIMEOUT_S,
            options=(
                f"-c statement_timeout={_POSTGRES_TIMEOUT_S * 1000} "
                f"-c lock_timeout={_POSTGRES_TIMEOUT_S * 1000}"
            ),
        ) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            membership = connection.execute(
                _MEMBERSHIP_SQL, (_CAPTURE_ROLE,)
            ).fetchone()
            if membership is None or membership[0] is not True:
                raise RuntimeError(
                    "catalog credential is not a leo_capture role member"
                )
            connection.execute("SET ROLE leo_capture")
            row = connection.execute(_DRAIN_READY_SQL).fetchone()
        if row is None or not isinstance(row[0], bool):
            raise RuntimeError("capture-analysis drain gate returned no decision")
        return row[0]
