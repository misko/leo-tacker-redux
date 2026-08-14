from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.contracts.core import ArtifactRef, JobId, SchemaRef
from leo_flow.jobs import JobPayload, JobState, JobType, StaleLeaseError
from leo_flow.jobs.postgres_repository import (
    PostgresJobLeaseRepository,
    connection_factory,
)
from testkit import digest


def payload(index: int) -> JobPayload:
    return JobPayload.create(
        SchemaRef("org.leo-flow.job.test"), {"recording_id": f"rec_{index:02d}"}
    )


def enqueue(repository: PostgresJobLeaseRepository, count: int = 1) -> None:
    for index in range(count):
        repository.enqueue(
            JobId(f"job_{index:02d}"), JobType.RECORDING_ANALYSIS, payload(index)
        )


@pytest.mark.integration
def test_concurrent_skip_locked_claims_distinct_jobs(postgres_dsn: str) -> None:
    repository = PostgresJobLeaseRepository(connection_factory(postgres_dsn))
    enqueue(repository, 8)

    def claim(index: int):
        return PostgresJobLeaseRepository(connection_factory(postgres_dsn)).claim(
            (JobType.RECORDING_ANALYSIS,), f"worker-{index}", 10.0
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        leases = list(executor.map(claim, range(8)))
    assert all(lease is not None for lease in leases)
    assert len({lease.job_id for lease in leases if lease is not None}) == 8
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM job WHERE state = 'leased' AND lease_expires_utc > clock_timestamp()"
        ).fetchone() == (8,)


@pytest.mark.integration
def test_one_job_has_at_most_one_live_lease(postgres_dsn: str) -> None:
    repository = PostgresJobLeaseRepository(connection_factory(postgres_dsn))
    enqueue(repository)

    def claim(index: int):
        return PostgresJobLeaseRepository(connection_factory(postgres_dsn)).claim(
            (JobType.RECORDING_ANALYSIS,), f"worker-{index}", 10.0
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        leases = list(executor.map(claim, range(16)))
    assert sum(lease is not None for lease in leases) == 1


@pytest.mark.integration
def test_expiry_reclaim_fences_stale_completion(postgres_dsn: str) -> None:
    repository = PostgresJobLeaseRepository(connection_factory(postgres_dsn))
    enqueue(repository)
    first = repository.claim((JobType.RECORDING_ANALYSIS,), "old", 0.05)
    assert first is not None
    time.sleep(0.08)
    second = repository.claim((JobType.RECORDING_ANALYSIS,), "new", 2.0)
    assert second is not None
    assert second.lease_generation == first.lease_generation + 1
    result = ArtifactRef("result_01", digest("result"))
    with pytest.raises(StaleLeaseError):
        repository.complete(
            first.job_id, first.lease_token, first.lease_generation, result
        )
    repository.complete(
        second.job_id, second.lease_token, second.lease_generation, result
    )
    with psycopg.connect(postgres_dsn) as connection:
        row = connection.execute(
            "SELECT state, lease_generation, result_ref FROM job WHERE job_id = %s",
            (str(first.job_id),),
        ).fetchone()
    assert row is not None
    assert row[0] == "succeeded"
    assert row[1] == second.lease_generation
    assert row[2]["artifact_id"] == "result_01"


@pytest.mark.integration
def test_exactly_one_completion_is_exposed(postgres_dsn: str) -> None:
    repository = PostgresJobLeaseRepository(connection_factory(postgres_dsn))
    enqueue(repository)
    lease = repository.claim((JobType.RECORDING_ANALYSIS,), "winner", 2.0)
    assert lease is not None
    result = ArtifactRef("result_01", digest("result"))
    repository.complete(lease.job_id, lease.lease_token, lease.lease_generation, result)
    with pytest.raises(StaleLeaseError):
        repository.complete(
            lease.job_id, lease.lease_token, lease.lease_generation, result
        )
    assert repository.claim((JobType.RECORDING_ANALYSIS,), "later", 2.0) is None
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM job WHERE state = 'succeeded' AND result_ref IS NOT NULL"
        ).fetchone() == (1,)


@pytest.mark.integration
def test_park_is_exact_terminal_state_and_never_reclaimed(postgres_dsn: str) -> None:
    repository = PostgresJobLeaseRepository(connection_factory(postgres_dsn))
    enqueue(repository)
    lease = repository.claim((JobType.RECORDING_ANALYSIS,), "worker", 10.0)
    assert lease is not None

    repository.park(
        lease.job_id,
        lease.lease_token,
        lease.lease_generation,
        "operator_action_required",
    )

    snapshot = repository.snapshot(lease.job_id)
    assert snapshot == repository.snapshot(lease.job_id)
    assert snapshot.state is JobState.PARKED
    assert snapshot.park_reason == "operator_action_required"
    assert snapshot.parked_at_utc_ns is not None
    assert snapshot.result_ref is None
    assert snapshot.last_error is None
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            """
            SELECT state, lease_token, lease_expires_utc, result_ref, last_error
              FROM job WHERE job_id = %s
            """,
            (str(lease.job_id),),
        ).fetchone() == ("parked", None, None, None, None)
        connection.execute(
            "UPDATE job SET available_at_utc = clock_timestamp() - interval '100 years'"
        )
    assert repository.claim((JobType.RECORDING_ANALYSIS,), "later", 10.0) is None


@pytest.mark.integration
def test_park_is_token_generation_and_expiry_fenced(postgres_dsn: str) -> None:
    repository = PostgresJobLeaseRepository(connection_factory(postgres_dsn))
    enqueue(repository)
    first = repository.claim((JobType.RECORDING_ANALYSIS,), "first", 0.05)
    assert first is not None
    time.sleep(0.08)
    second = repository.claim((JobType.RECORDING_ANALYSIS,), "second", 0.05)
    assert second is not None

    for token, generation in (
        (first.lease_token, first.lease_generation),
        ("lease_wrong", second.lease_generation),
        (second.lease_token, first.lease_generation),
    ):
        with pytest.raises(StaleLeaseError):
            repository.park(second.job_id, token, generation, "stale_lease")
    time.sleep(0.08)
    with pytest.raises(StaleLeaseError):
        repository.park(
            second.job_id,
            second.lease_token,
            second.lease_generation,
            "expired_lease",
        )
    assert repository.snapshot(second.job_id).state is JobState.LEASED


@pytest.mark.integration
def test_analysis_role_uses_functions_but_cannot_mutate_or_requeue_directly(
    postgres_dsn: str,
) -> None:
    def analysis_connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        connection.execute("SET ROLE leo_analysis")
        return connection

    repository = PostgresJobLeaseRepository(analysis_connect)
    repository.enqueue(JobId("job_role"), JobType.RECORDING_ANALYSIS, payload(9))
    lease = repository.claim((JobType.RECORDING_ANALYSIS,), "worker", 10.0)
    assert lease is not None
    repository.heartbeat(lease.job_id, lease.lease_token, lease.lease_generation, 10.0)
    repository.park(
        lease.job_id,
        lease.lease_token,
        lease.lease_generation,
        "operator_action_required",
    )
    assert repository.snapshot(lease.job_id).state is JobState.PARKED

    repository.enqueue(
        JobId("job_role_normal"), JobType.RECORDING_ANALYSIS, payload(10)
    )
    normal = repository.claim((JobType.RECORDING_ANALYSIS,), "worker", 10.0)
    assert normal is not None
    repository.fail(
        normal.job_id,
        normal.lease_token,
        normal.lease_generation,
        "transient",
        None,
    )
    retried = repository.claim((JobType.RECORDING_ANALYSIS,), "worker", 10.0)
    assert retried is not None and retried.attempt == 2
    result = ArtifactRef("result_role", digest("role-result"))
    repository.complete(
        retried.job_id, retried.lease_token, retried.lease_generation, result
    )
    succeeded = repository.snapshot(retried.job_id)
    assert succeeded.state is JobState.SUCCEEDED
    assert succeeded.result_ref == result

    with (
        psycopg.connect(postgres_dsn) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        connection.execute("SET ROLE leo_analysis")
        connection.execute("UPDATE job SET state = 'ready' WHERE job_id = 'job_role'")
    with (
        psycopg.connect(postgres_dsn) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        connection.execute("SET ROLE leo_analysis")
        connection.execute(
            """
            INSERT INTO job
                (job_id, job_type, payload_schema_id, payload_schema_version,
                 payload, state, available_at_utc)
            VALUES ('job_bypass', 'recording_analysis', 'x', '0.1', '{}',
                    'ready', clock_timestamp())
            """
        )


@pytest.mark.integration
def test_job_function_and_table_roles_are_narrow(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        assert not connection.execute(
            "SELECT has_table_privilege('leo_analysis', 'job', 'INSERT')"
        ).fetchone()[0]
        assert not connection.execute(
            "SELECT has_table_privilege('leo_analysis', 'job', 'UPDATE')"
        ).fetchone()[0]
        assert connection.execute(
            """
            SELECT has_function_privilege(
                'leo_analysis', 'park_job(text,text,bigint,text)', 'EXECUTE')
            """
        ).fetchone()[0]
        assert connection.execute(
            """
            SELECT has_function_privilege(
                'leo_analysis',
                'lock_active_job_lease(text,text,text,bigint)', 'EXECUTE')
            """
        ).fetchone()[0]
        for role in ("leo_capture", "leo_dashboard"):
            assert not connection.execute(
                """
                SELECT has_function_privilege(
                    %s, 'park_job(text,text,bigint,text)', 'EXECUTE')
                """,
                (role,),
            ).fetchone()[0]
            assert not connection.execute(
                "SELECT has_table_privilege(%s, 'job', 'UPDATE')", (role,)
            ).fetchone()[0]
            assert not connection.execute(
                """
                SELECT has_function_privilege(
                    %s, 'lock_active_job_lease(text,text,text,bigint)', 'EXECUTE')
                """,
                (role,),
            ).fetchone()[0]
        assert connection.execute(
            "SELECT to_regprocedure('requeue_job(text,text)') IS NULL"
        ).fetchone()[0]
