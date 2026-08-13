from __future__ import annotations

from dataclasses import dataclass

import pytest

from leo_flow.contracts.core import ArtifactRef, JobId, SchemaRef
from leo_flow.jobs import InMemoryJobLeaseRepository, JobPayload, JobType
from leo_flow.jobs.memory import JobState
from leo_flow.services import FencedAnalysisCycle
from testkit import digest

RESULT = ArtifactRef("result_features_1", digest("features"))


@dataclass
class Processor:
    fail: bool = False

    def process(self, lease) -> ArtifactRef:
        if self.fail:
            raise ValueError("invalid recording reference")
        assert lease.job_type is JobType.RECORDING_ANALYSIS
        return RESULT


def repository() -> InMemoryJobLeaseRepository:
    repo = InMemoryJobLeaseRepository(
        now_utc_ns=lambda: 100, token_factory=lambda: "lease-token"
    )
    repo.enqueue(
        JobId("job_recording_1"),
        JobType.RECORDING_ANALYSIS,
        JobPayload.create(
            SchemaRef("leo.job.recording-analysis.v0.1"),
            {"recording_ref": "recording_1"},
        ),
    )
    return repo


def test_fenced_worker_completes_exactly_one_published_analysis_result() -> None:
    repo = repository()
    cycle = FencedAnalysisCycle(
        repo, Processor(), worker_id="analysis-1", lease_ttl_s=30
    )
    assert cycle.process_one_job()
    snapshot = repo.snapshot(JobId("job_recording_1"))
    assert snapshot.state is JobState.SUCCEEDED
    assert snapshot.result_ref == RESULT
    assert not cycle.process_one_job()


def test_fenced_worker_records_failure_before_propagating_fault() -> None:
    repo = repository()
    cycle = FencedAnalysisCycle(
        repo, Processor(fail=True), worker_id="analysis-1", lease_ttl_s=30
    )
    with pytest.raises(ValueError, match="invalid recording"):
        cycle.process_one_job()
    snapshot = repo.snapshot(JobId("job_recording_1"))
    assert snapshot.state is JobState.FAILED
    assert snapshot.last_error == "ValueError: processor failed"
