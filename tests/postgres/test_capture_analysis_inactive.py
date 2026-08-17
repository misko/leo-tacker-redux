from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from leo_flow.adapters.capture_analysis_inactive_postgres import (
    PostgresCaptureAnalysisInactiveGate,
)
from tests.postgres.test_feature_projection_work import (
    _capture_gate_login,
    _publish,
)


@pytest.mark.integration
def test_inactive_gate_allows_pending_backlog_but_blocks_current_job_lease(
    postgres_dsn: str,
) -> None:
    with _capture_gate_login(postgres_dsn, member=True) as capture_dsn:
        gate = PostgresCaptureAnalysisInactiveGate(capture_dsn)
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                INSERT INTO job(
                    job_id, job_type, payload_schema_id, payload_schema_version,
                    payload, state, available_at_utc)
                VALUES ('job_inactive_pending', 'recording_analysis', 'test', '0.1',
                        '{}', 'ready', clock_timestamp())
                """
            )
        assert gate.ready()

        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                UPDATE job
                   SET state = 'leased', lease_token = 'inactive-test',
                       lease_generation = 1,
                       lease_expires_utc = clock_timestamp() + interval '1 hour'
                 WHERE job_id = 'job_inactive_pending'
                """
            )
        assert not gate.ready()

        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                UPDATE job
                   SET lease_expires_utc = clock_timestamp() - interval '1 second'
                 WHERE job_id = 'job_inactive_pending'
                """
            )
        assert gate.ready()


@pytest.mark.integration
def test_inactive_gate_blocks_current_projection_lease_and_has_exact_acl(
    postgres_dsn: str, tmp_path: Path
) -> None:
    _publish(postgres_dsn, tmp_path / "cas")
    with _capture_gate_login(postgres_dsn, member=True) as capture_dsn:
        gate = PostgresCaptureAnalysisInactiveGate(capture_dsn)
        assert gate.ready()
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                UPDATE feature_projection_work
                   SET state = 'leased', lease_token = 'projection-inactive-test',
                       lease_generation = lease_generation + 1,
                       lease_expires_utc = clock_timestamp() + interval '1 hour'
                """
            )
        assert not gate.ready()
        with psycopg.connect(postgres_dsn) as connection:
            privileges = connection.execute(
                """
                SELECT has_function_privilege(
                           'leo_capture', 'capture_analysis_inactive()', 'EXECUTE'),
                       has_function_privilege(
                           'leo_analysis', 'capture_analysis_inactive()', 'EXECUTE'),
                       has_function_privilege(
                           'leo_dashboard', 'capture_analysis_inactive()', 'EXECUTE'),
                       has_function_privilege(
                           'leo_maintenance', 'capture_analysis_inactive()', 'EXECUTE'),
                       NOT EXISTS (
                           SELECT 1
                             FROM pg_proc AS procedure,
                                  LATERAL aclexplode(
                                      COALESCE(
                                          procedure.proacl,
                                          acldefault('f', procedure.proowner)
                                      )
                                  ) AS privilege
                            WHERE procedure.oid =
                                      'capture_analysis_inactive()'::regprocedure
                              AND privilege.grantee = 0
                              AND privilege.privilege_type = 'EXECUTE'
                       )
                """
            ).fetchone()
        assert privileges == (True, False, False, False, True)
