from __future__ import annotations

from dataclasses import replace

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.dashboard_batch_postgres import (
    PostgresCaptureBatchProjectionWriter,
)
from leo_flow.adapters.dashboard_recording_evidence_postgres import (
    PostgresRecordingEvidenceContextRepositoryV0_1,
)
from leo_flow.contracts.core import CaptureBatchId, RecordingId
from tests.dashboard._fixtures import BATCH_READY, capture_batches


def _role_connect(postgres_dsn: str, role: str):
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        connection.execute(f"SET ROLE {role}")
        return connection

    return connect


@pytest.mark.integration
def test_v16_batch_context_read_succeeds_with_existing_dashboard_acl(
    postgres_dsn: str,
) -> None:
    PostgresCaptureBatchProjectionWriter(
        _role_connect(postgres_dsn, "leo_capture")
    ).publish(capture_batches()[0].view)
    repository = PostgresRecordingEvidenceContextRepositoryV0_1(
        _role_connect(postgres_dsn, "leo_dashboard"),
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    assert repository._batch_ids(RecordingId("rec_ready_a")) == (BATCH_READY,)
    assert repository._batch_ids(RecordingId("rec_absent")) == ()

    with psycopg.connect(postgres_dsn) as connection:
        privileges = connection.execute(
            """
            SELECT has_table_privilege(
                       'leo_dashboard',
                       'dashboard_capture_batch_projection', 'SELECT'),
                   has_table_privilege(
                       'leo_dashboard',
                       'dashboard_capture_attempt_projection', 'SELECT'),
                   has_function_privilege(
                       'leo_dashboard',
                       'resolve_dashboard_capture_batches_for_recording(text)',
                       'EXECUTE')
            """
        ).fetchone()
    assert privileges == (True, True, False)


@pytest.mark.integration
def test_v16_batch_context_is_limited_to_ambiguity_witness(
    postgres_dsn: str,
) -> None:
    base = capture_batches()[0].view
    writer = PostgresCaptureBatchProjectionWriter(
        _role_connect(postgres_dsn, "leo_capture")
    )
    for suffix in ("a", "b", "c"):
        writer.publish(
            replace(base, batch_id=CaptureBatchId(f"cbatch_duplicate_{suffix}"))
        )
    repository = PostgresRecordingEvidenceContextRepositoryV0_1(
        _role_connect(postgres_dsn, "leo_dashboard"),
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    assert repository._batch_ids(RecordingId("rec_ready_a")) == (
        CaptureBatchId("cbatch_duplicate_a"),
        CaptureBatchId("cbatch_duplicate_b"),
    )
