from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from leo_flow.adapters.capture_analysis_drain_postgres import (
    PostgresCaptureAnalysisDrainGate,
)
from tests.postgres.test_feature_projection_work import _capture_gate_login
from tests.postgres.test_waterfall_analysis_atomic import _claimed, _committer


@pytest.mark.integration
def test_full_drain_blocks_retryable_waterfall_jobs_and_allows_terminal_states(
    postgres_dsn: str,
) -> None:
    with _capture_gate_login(postgres_dsn, member=True) as capture_dsn:
        gate = PostgresCaptureAnalysisDrainGate(capture_dsn)
        assert gate.ready()
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                INSERT INTO job(
                    job_id, job_type, payload_schema_id, payload_schema_version,
                    payload, state, available_at_utc)
                VALUES ('job_waterfall_drain_states', 'waterfall_analysis',
                        'test', '0.1', '{}', 'ready', clock_timestamp())
                """
            )
        assert not gate.ready()

        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                UPDATE job
                   SET state='leased', lease_token='waterfall-drain-job',
                       lease_generation=1,
                       lease_expires_utc=clock_timestamp()+interval '1 hour'
                 WHERE job_id='job_waterfall_drain_states'
                """
            )
        assert not gate.ready()

        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                UPDATE job
                   SET state='failed', lease_token=NULL, lease_expires_utc=NULL,
                       last_error='retryable'
                 WHERE job_id='job_waterfall_drain_states'
                """
            )
        assert not gate.ready()

        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                UPDATE job
                   SET state='parked', last_error=NULL,
                       park_reason='operator_parked',
                       parked_at_utc=clock_timestamp()
                 WHERE job_id='job_waterfall_drain_states'
                """
            )
        assert gate.ready()

        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                UPDATE job
                   SET state='succeeded', park_reason=NULL, parked_at_utc=NULL
                 WHERE job_id='job_waterfall_drain_states'
                """
            )
        assert gate.ready()


@pytest.mark.integration
def test_full_drain_blocks_retryable_waterfall_projection_work(
    postgres_dsn: str, tmp_path: Path
) -> None:
    _, lease, prepared = _claimed(postgres_dsn)
    _committer(postgres_dsn, tmp_path / "cas").commit_waterfall(lease, prepared)

    with _capture_gate_login(postgres_dsn, member=True) as capture_dsn:
        gate = PostgresCaptureAnalysisDrainGate(capture_dsn)
        assert not gate.ready()

        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                UPDATE waterfall_projection_work
                   SET state='leased', lease_token='waterfall-drain-work',
                       lease_generation=1,
                       lease_expires_utc=clock_timestamp()+interval '1 hour'
                """
            )
        assert not gate.ready()

        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                UPDATE waterfall_projection_work
                   SET state='failed', lease_token=NULL, lease_expires_utc=NULL,
                       last_error='retryable'
                """
            )
        assert not gate.ready()

        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                UPDATE waterfall_projection_work
                   SET state='parked', last_error=NULL,
                       park_reason='operator_parked',
                       parked_at_utc=clock_timestamp()
                """
            )
        assert gate.ready()

        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                UPDATE waterfall_projection_work
                   SET state='succeeded', park_reason=NULL, parked_at_utc=NULL,
                       projected_at_utc=clock_timestamp()
                """
            )
        assert gate.ready()


@pytest.mark.integration
def test_full_drain_replacement_retains_exact_owner_security_and_acl(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        row = connection.execute(
            """
            SELECT pg_get_userbyid(procedure.proowner), procedure.prosecdef,
                   procedure.proconfig,
                   has_function_privilege(
                       'leo_capture', 'capture_analysis_drain_ready()', 'EXECUTE'),
                   has_function_privilege(
                       'leo_analysis', 'capture_analysis_drain_ready()', 'EXECUTE'),
                   has_function_privilege(
                       'leo_dashboard', 'capture_analysis_drain_ready()', 'EXECUTE'),
                   has_function_privilege(
                       'leo_maintenance', 'capture_analysis_drain_ready()', 'EXECUTE'),
                   NOT EXISTS (
                       SELECT 1
                         FROM aclexplode(
                             COALESCE(
                                 procedure.proacl,
                                 acldefault('f', procedure.proowner)
                             )
                         ) AS privilege
                        WHERE privilege.grantee = 0
                          AND privilege.privilege_type = 'EXECUTE'
                   )
              FROM pg_proc AS procedure
             WHERE procedure.oid =
                   'capture_analysis_drain_ready()'::regprocedure
            """
        ).fetchone()

    assert row == (
        "leo_routine_owner",
        True,
        ["search_path=pg_catalog, pg_temp"],
        True,
        False,
        False,
        False,
        True,
    )
