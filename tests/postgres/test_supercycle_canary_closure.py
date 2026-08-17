from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.supercycle_canary_closure_postgres import (
    PostgresSupercycleCanaryClosureReaderV1,
)
from leo_flow.contracts.core import CaptureBatchId, Digest, JobId, RecordingId
from leo_flow.contracts.deferred_analysis import DeferredAnalysisWindowV1


def _connect(postgres_dsn: str, role: str):
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        connection.execute(f"SET ROLE {role}")
        return connection

    return connect


@pytest.mark.integration
def test_empty_exact_canary_scope_is_readable_only_through_reviewed_roles(
    postgres_dsn: str,
) -> None:
    window = DeferredAnalysisWindowV1(
        Digest.sha256(b"postgres-canary-definition"),
        0,
        tuple(CaptureBatchId(f"cbatch_pg_canary_{index:02d}") for index in range(36)),
        tuple(RecordingId(f"rec_pg_canary_{index:02d}") for index in range(72)),
        tuple(Digest.sha256(f"recording-{index}".encode()) for index in range(72)),
        tuple(JobId(f"job_pg_feature_{index:02d}") for index in range(72)),
        tuple(JobId(f"job_pg_waterfall_{index:02d}") for index in range(72)),
        tuple(JobId(f"job_pg_suite_{index:02d}") for index in range(72)),
    )
    reader = PostgresSupercycleCanaryClosureReaderV1(
        _connect(postgres_dsn, "leo_analysis"),
        _connect(postgres_dsn, "leo_dashboard"),
    )

    assert reader.counts(str(window.definition_digest), window) == (0, 0, 0, 0)
