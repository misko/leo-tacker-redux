from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from leo_flow.contracts.core import ArtifactRef, JobId, SchemaRef
from leo_flow.jobs import JobPayload, JobType
from leo_flow.jobs.postgres_repository import (
    PostgresJobLeaseRepository,
    StaleLeaseError,
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
