from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from leo_flow.contracts.core import ArtifactRef, JobId, SchemaRef, UtcNs
from leo_flow.jobs import InMemoryJobLeaseRepository, JobPayload, JobType
from leo_flow.jobs.contracts import JobLease
from leo_flow.jobs.memory import JobState
from leo_flow.services import TypedAnalysisRouterCycle
from testkit import digest


@dataclass
class _Executor:
    jobs: InMemoryJobLeaseRepository
    expected: JobType
    calls: list[JobLease] = field(default_factory=list)

    def execute(self, lease: JobLease) -> None:
        assert lease.job_type is self.expected
        self.calls.append(lease)
        self.jobs.complete(
            lease.job_id,
            lease.lease_token,
            lease.lease_generation,
            ArtifactRef(f"result_{self.expected.value}", digest(self.expected.value)),
        )


def _jobs() -> InMemoryJobLeaseRepository:
    return InMemoryJobLeaseRepository(
        now_utc_ns=lambda: 100,
        token_factory=lambda: "lease_router",
    )


def _enqueue(jobs: InMemoryJobLeaseRepository, job_type: JobType, suffix: str) -> None:
    jobs.enqueue(
        JobId(f"job_{suffix}"),
        job_type,
        JobPayload.create(SchemaRef(f"job-{suffix}"), {"kind": job_type.value}),
        available_at_utc_ns=UtcNs(0),
    )


def test_router_claims_and_dispatches_each_implemented_type_exactly_once() -> None:
    jobs = _jobs()
    recording = _Executor(jobs, JobType.RECORDING_ANALYSIS)
    model = _Executor(jobs, JobType.MODEL_ANALYSIS)
    ephemeris = _Executor(jobs, JobType.EPHEMERIS_RETRIEVAL)
    backfill = _Executor(jobs, JobType.EPHEMERIS_LINK_BACKFILL)
    for job_type, suffix in (
        (JobType.RECORDING_ANALYSIS, "a_recording"),
        (JobType.MODEL_ANALYSIS, "b_model"),
        (JobType.EPHEMERIS_RETRIEVAL, "c_ephemeris"),
        (JobType.EPHEMERIS_LINK_BACKFILL, "d_backfill"),
    ):
        _enqueue(jobs, job_type, suffix)
    executors = {
        JobType.RECORDING_ANALYSIS: recording,
        JobType.MODEL_ANALYSIS: model,
        JobType.EPHEMERIS_RETRIEVAL: ephemeris,
        JobType.EPHEMERIS_LINK_BACKFILL: backfill,
    }
    router = TypedAnalysisRouterCycle(
        jobs,
        executors=executors,
        worker_id="router",
        lease_ttl_s=10,
    )

    assert [router.process_one_job() for _ in range(4)] == [True] * 4
    assert not router.process_one_job()
    assert router.claimed_types == tuple(sorted(executors, key=lambda kind: kind.value))
    assert all(
        len(executor.calls) == 1 for executor in (recording, model, ephemeris, backfill)
    )


def test_router_never_claims_a_job_without_an_installed_executor() -> None:
    jobs = _jobs()
    _enqueue(jobs, JobType.EPHEMERIS_LINK_BACKFILL, "backfill")
    _enqueue(jobs, JobType.RECORDING_ANALYSIS, "recording")
    recording = _Executor(jobs, JobType.RECORDING_ANALYSIS)
    router = TypedAnalysisRouterCycle(
        jobs,
        executors={JobType.RECORDING_ANALYSIS: recording},
        worker_id="router",
        lease_ttl_s=10,
    )

    assert router.process_one_job()
    assert not router.process_one_job()
    assert len(recording.calls) == 1
    assert jobs.snapshot(JobId("job_backfill")).state is JobState.READY


class _FailingExecutor:
    def __init__(self, jobs: InMemoryJobLeaseRepository) -> None:
        self._jobs = jobs

    def execute(self, lease: JobLease) -> None:
        self._jobs.fail(
            lease.job_id,
            lease.lease_token,
            lease.lease_generation,
            "executor-owned-failure",
            UtcNs(1_000),
        )
        raise RuntimeError("executor fault")


def test_router_does_not_complete_or_refail_after_executor_fault() -> None:
    jobs = _jobs()
    _enqueue(jobs, JobType.MODEL_ANALYSIS, "model")
    router = TypedAnalysisRouterCycle(
        jobs,
        executors={JobType.MODEL_ANALYSIS: _FailingExecutor(jobs)},
        worker_id="router",
        lease_ttl_s=10,
    )

    with pytest.raises(RuntimeError, match="executor fault"):
        router.process_one_job()
    snapshot = jobs.snapshot(JobId("job_model"))
    assert snapshot.state is JobState.FAILED
    assert snapshot.last_error == "executor-owned-failure"


def test_router_lifecycle_hooks_are_bounded_and_explicit() -> None:
    events: list[object] = []
    jobs = _jobs()
    executor = _Executor(jobs, JobType.RECORDING_ANALYSIS)
    router = TypedAnalysisRouterCycle(
        jobs,
        executors={JobType.RECORDING_ANALYSIS: executor},
        worker_id="router",
        lease_ttl_s=10,
        preflight=lambda: events.append("preflight"),
        close=lambda timeout: events.append(timeout),
    )
    router.preflight()
    router.close(2.5)
    assert events == ["preflight", 2.5]
