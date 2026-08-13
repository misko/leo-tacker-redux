from __future__ import annotations

import pytest

from leo_flow.analysis.ephemeris.catalog import (
    ArchivedEphemerisSnapshot,
    InMemoryEphemerisSnapshotCatalog,
)
from leo_flow.analysis.ephemeris.providers import RetryableProviderError
from leo_flow.analysis.ephemeris.scheduling import EphemerisRetryPolicy
from leo_flow.analysis.ephemeris.worker import EphemerisRetrievalWorker
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    EphemerisRetrievalId,
    EphemerisSnapshotId,
    JobId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.ephemeris import (
    EphemerisSnapshot,
    EphemerisSource,
    ValidationResult,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.jobs import InMemoryJobLeaseRepository, JobPayload, JobType
from leo_flow.jobs.memory import JobState
from testkit import digest


def ref(name: str) -> ObjectRef:
    return ObjectRef(
        digest(name), len(name), "application/octet-stream", name, f"memory:{name}"
    )


def archived() -> ArchivedEphemerisSnapshot:
    snapshot = EphemerisSnapshot(
        SchemaRef(EphemerisSnapshot.SCHEMA_ID, V0_1),
        EphemerisSnapshotId("eph_one"),
        EphemerisRetrievalId("ephret_one"),
        EphemerisSource.HUGGING_FACE,
        "starlink",
        UtcNs(10),
        ref("raw"),
        ref("normalized"),
        ArtifactRef("parser-v1", digest("parser")),
        1,
        digest("norad"),
        UtcNs(1),
        UtcNs(2),
        ValidationResult(True, ArtifactRef("policy-v1", digest("policy"))),
        "attribution",
    )
    return ArchivedEphemerisSnapshot(
        snapshot, ref("provenance"), digest("request").value
    )


def payload(**changes: object) -> JobPayload:
    value: dict[str, object] = {
        "retrieval_id": "ephret_one",
        "source": "huggingface",
        "scope": "starlink",
        "request_spec": "hf-starlink-v1",
        "slot_utc_ns": 0,
    }
    value.update(changes)
    return JobPayload.create(SchemaRef("org.leo-flow.ephemeris-retrieval-job"), value)


class Preparer:
    def __init__(self, result: ArchivedEphemerisSnapshot | Exception) -> None:
        self.result = result
        self.requests = []

    def prepare(self, request):
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeCommitter:
    def __init__(self, jobs, catalog) -> None:
        self.jobs = jobs
        self.catalog = catalog
        self.calls = 0

    def publish_and_complete(self, lease, value, result_ref) -> None:
        self.calls += 1
        self.catalog.publish(value)
        self.jobs.complete(
            lease.job_id, lease.lease_token, lease.lease_generation, result_ref
        )


def leased_job(jobs, job_payload: JobPayload | None = None):
    jobs.enqueue(
        JobId("job_one"),
        JobType.EPHEMERIS_RETRIEVAL,
        job_payload or payload(),
        available_at_utc_ns=UtcNs(0),
    )
    lease = jobs.claim((JobType.EPHEMERIS_RETRIEVAL,), "worker", 10.0)
    assert lease is not None
    return lease


def test_worker_parses_safe_payload_and_commits_snapshot_before_success() -> None:
    jobs = InMemoryJobLeaseRepository(now_utc_ns=lambda: 0)
    catalog = InMemoryEphemerisSnapshotCatalog()
    preparer = Preparer(archived())
    committer = FakeCommitter(jobs, catalog)
    lease = leased_job(jobs)
    result = EphemerisRetrievalWorker(
        preparer,
        committer,
        jobs,
        EphemerisRetryPolicy(),
        now_utc_ns=lambda: 0,
    ).execute(lease)

    assert result == archived()
    assert preparer.requests[0].retrieval_id == EphemerisRetrievalId("ephret_one")
    assert catalog.get(EphemerisSnapshotId("eph_one")) == result
    snapshot = jobs.snapshot(JobId("job_one"))
    assert snapshot.state is JobState.SUCCEEDED
    assert snapshot.result_ref is not None
    assert snapshot.result_ref.artifact_id == "eph_one"


def test_worker_failure_stores_policy_code_not_provider_detail() -> None:
    jobs = InMemoryJobLeaseRepository(now_utc_ns=lambda: 0)
    error = RetryableProviderError(
        EphemerisSource.HUGGING_FACE, "secret diagnostic", retry_after_s=7
    )
    preparer = Preparer(error)
    committer = FakeCommitter(jobs, InMemoryEphemerisSnapshotCatalog())
    lease = leased_job(jobs)
    worker = EphemerisRetrievalWorker(
        preparer,
        committer,
        jobs,
        EphemerisRetryPolicy(),
        now_utc_ns=lambda: 100,
    )

    with pytest.raises(RetryableProviderError):
        worker.execute(lease)
    snapshot = jobs.snapshot(JobId("job_one"))
    assert snapshot.state is JobState.FAILED
    assert snapshot.last_error == "provider_rate_limit"
    assert committer.calls == 0


def test_worker_rejects_payload_fields_that_could_smuggle_credentials() -> None:
    jobs = InMemoryJobLeaseRepository(now_utc_ns=lambda: 0)
    preparer = Preparer(archived())
    committer = FakeCommitter(jobs, InMemoryEphemerisSnapshotCatalog())
    lease = leased_job(jobs, payload(password="not-stored"))
    worker = EphemerisRetrievalWorker(
        preparer,
        committer,
        jobs,
        EphemerisRetryPolicy(),
        now_utc_ns=lambda: 100,
    )

    with pytest.raises(ValueError, match="payload fields"):
        worker.execute(lease)
    assert jobs.snapshot(JobId("job_one")).last_error == "processing_failure"
    assert preparer.requests == []
