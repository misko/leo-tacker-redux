from __future__ import annotations

import time
from typing import cast

from leo_flow.contracts.core import ArtifactRef, JobId, SchemaRef
from leo_flow.jobs import InMemoryJobLeaseRepository, JobPayload, JobType
from leo_flow.jobs.contracts import JobLease, JobState
from leo_flow.services.starlink_suite_analysis import (
    FencedStarlinkSuiteAnalysisWorkerV0_2,
    PreparedStarlinkSuiteAnalysisV0_2,
)
from testkit import digest


class HeartbeatRepository(InMemoryJobLeaseRepository):
    def __init__(self) -> None:
        super().__init__()
        self.heartbeat_count = 0

    def heartbeat(
        self, job_id: JobId, lease_token: str, generation: int, ttl_s: float
    ) -> JobLease:
        self.heartbeat_count += 1
        return super().heartbeat(job_id, lease_token, generation, ttl_s)


class SlowPreparer:
    def prepare(self, lease: JobLease) -> PreparedStarlinkSuiteAnalysisV0_2:
        del lease
        time.sleep(0.12)
        return cast(PreparedStarlinkSuiteAnalysisV0_2, object())


class Committer:
    def __init__(self, jobs: HeartbeatRepository) -> None:
        self._jobs = jobs

    def commit_starlink_suite(
        self, lease: JobLease, prepared: PreparedStarlinkSuiteAnalysisV0_2
    ) -> ArtifactRef:
        del prepared
        result = ArtifactRef("result_starlink_heartbeat", digest("heartbeat"))
        self._jobs.complete(
            lease.job_id,
            lease.lease_token,
            lease.lease_generation,
            result,
        )
        return result


class FailedHeartbeatRepository(HeartbeatRepository):
    def heartbeat(
        self, job_id: JobId, lease_token: str, generation: int, ttl_s: float
    ) -> JobLease:
        del job_id, lease_token, generation, ttl_s
        self.heartbeat_count += 1
        raise RuntimeError("heartbeat unavailable")


def test_long_suite_compute_renews_its_fenced_lease_until_commit() -> None:
    jobs = HeartbeatRepository()
    job_id = JobId("job_starlink_heartbeat")
    jobs.enqueue(
        job_id,
        JobType.STARLINK_SUITE_ANALYSIS,
        JobPayload.create(SchemaRef("test.starlink-suite-job"), {}),
    )
    lease = jobs.claim((JobType.STARLINK_SUITE_ANALYSIS,), "worker", 0.05)
    assert lease is not None

    result = FencedStarlinkSuiteAnalysisWorkerV0_2(
        jobs,
        SlowPreparer(),
        Committer(jobs),
        worker_id="worker",
        lease_ttl_s=0.05,
        heartbeat_interval_s=0.01,
    ).execute(lease)

    assert result == ArtifactRef("result_starlink_heartbeat", digest("heartbeat"))
    assert jobs.heartbeat_count >= 5


def test_heartbeat_failure_prevents_unfenced_publication() -> None:
    jobs = FailedHeartbeatRepository()
    job_id = JobId("job_starlink_failed_heartbeat")
    jobs.enqueue(
        job_id,
        JobType.STARLINK_SUITE_ANALYSIS,
        JobPayload.create(SchemaRef("test.starlink-suite-job"), {}),
    )
    lease = jobs.claim((JobType.STARLINK_SUITE_ANALYSIS,), "worker", 1.0)
    assert lease is not None

    result = FencedStarlinkSuiteAnalysisWorkerV0_2(
        jobs,
        SlowPreparer(),
        Committer(jobs),
        worker_id="worker",
        lease_ttl_s=1.0,
        heartbeat_interval_s=0.01,
    ).execute(lease)

    assert result is None
    assert jobs.heartbeat_count == 1
    assert jobs.snapshot(job_id).state is JobState.FAILED
