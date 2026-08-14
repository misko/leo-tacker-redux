from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.ephemeris.catalog import (
    ArchivedEphemerisSnapshot,
    InMemoryEphemerisSnapshotCatalog,
)
from leo_flow.analysis.ephemeris.providers import (
    AuthenticationProviderError,
    ProviderResponseError,
    RetryableProviderError,
)
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
from leo_flow.jobs import (
    InMemoryJobLeaseRepository,
    JobPayload,
    JobType,
    StaleLeaseError,
)
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


class Clock:
    def __init__(self, value: int = 0) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class MutationCountingRepository:
    def __init__(self, delegate: InMemoryJobLeaseRepository) -> None:
        self.delegate = delegate
        self.fail_calls = 0
        self.park_calls = 0

    def claim(self, types, worker_id, ttl_s):
        return self.delegate.claim(types, worker_id, ttl_s)

    def heartbeat(self, job_id, lease_token, generation, ttl_s):
        return self.delegate.heartbeat(job_id, lease_token, generation, ttl_s)

    def complete(self, job_id, lease_token, generation, result_ref) -> None:
        self.delegate.complete(job_id, lease_token, generation, result_ref)

    def fail(self, job_id, lease_token, generation, reason, retry_at_utc_ns) -> None:
        self.fail_calls += 1
        self.delegate.fail(job_id, lease_token, generation, reason, retry_at_utc_ns)

    def park(self, job_id, lease_token, generation, reason) -> None:
        self.park_calls += 1
        self.delegate.park(job_id, lease_token, generation, reason)


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


def test_retry_after_fails_until_exact_retry_without_storing_provider_detail() -> None:
    clock = Clock(100)
    jobs = InMemoryJobLeaseRepository(now_utc_ns=clock)
    error = RetryableProviderError(
        EphemerisSource.HUGGING_FACE,
        "secret diagnostic password=hunter2",
        retry_after_s=7,
    )
    preparer = Preparer(error)
    committer = FakeCommitter(jobs, InMemoryEphemerisSnapshotCatalog())
    lease = leased_job(jobs)
    worker = EphemerisRetrievalWorker(
        preparer,
        committer,
        jobs,
        EphemerisRetryPolicy(),
        now_utc_ns=clock,
    )

    with pytest.raises(RetryableProviderError):
        worker.execute(lease)
    snapshot = jobs.snapshot(JobId("job_one"))
    assert snapshot.state is JobState.FAILED
    assert snapshot.attempt == 1
    assert snapshot.last_error == "provider_rate_limit"
    assert "secret" not in repr(snapshot)
    assert committer.calls == 0

    clock.value = 7_000_000_099
    assert jobs.claim((JobType.EPHEMERIS_RETRIEVAL,), "early", 10.0) is None
    clock.value = 7_000_000_100
    retry = jobs.claim((JobType.EPHEMERIS_RETRIEVAL,), "retry", 10.0)
    assert retry is not None
    assert retry.attempt == 2


def test_transient_failure_uses_bounded_exponential_retry_per_attempt() -> None:
    clock = Clock(1_000)
    jobs = InMemoryJobLeaseRepository(now_utc_ns=clock)
    preparer = Preparer(
        RetryableProviderError(EphemerisSource.HUGGING_FACE, "server secret")
    )
    worker = EphemerisRetrievalWorker(
        preparer,
        FakeCommitter(jobs, InMemoryEphemerisSnapshotCatalog()),
        jobs,
        EphemerisRetryPolicy(initial_delay_s=10, maximum_delay_s=15),
        now_utc_ns=clock,
    )
    first = leased_job(jobs)

    with pytest.raises(RetryableProviderError):
        worker.execute(first)
    assert jobs.snapshot(first.job_id).last_error == "provider_transient"
    clock.value = 10_000_001_000
    second = jobs.claim((JobType.EPHEMERIS_RETRIEVAL,), "second", 10.0)
    assert second is not None and second.attempt == 2

    with pytest.raises(RetryableProviderError):
        worker.execute(second)
    clock.value = 25_000_000_999
    assert jobs.claim((JobType.EPHEMERIS_RETRIEVAL,), "early", 10.0) is None
    clock.value = 25_000_001_000
    third = jobs.claim((JobType.EPHEMERIS_RETRIEVAL,), "third", 10.0)
    assert third is not None and third.attempt == 3


@pytest.mark.parametrize(
    ("error", "reason"),
    (
        (
            AuthenticationProviderError(
                EphemerisSource.SPACE_TRACK, "password=operator-secret"
            ),
            "provider_authentication",
        ),
        (
            ProviderResponseError(
                EphemerisSource.HUGGING_FACE, "invalid secret response"
            ),
            "provider_response_invalid",
        ),
        (RuntimeError("processing secret"), "processing_failure"),
    ),
)
def test_nonretryable_failures_are_parked_with_stable_secret_free_reason(
    error: Exception, reason: str
) -> None:
    clock = Clock()
    jobs = InMemoryJobLeaseRepository(now_utc_ns=clock)
    lease = leased_job(jobs)
    worker = EphemerisRetrievalWorker(
        Preparer(error),
        FakeCommitter(jobs, InMemoryEphemerisSnapshotCatalog()),
        jobs,
        EphemerisRetryPolicy(),
        now_utc_ns=clock,
    )

    with pytest.raises(type(error)) as raised:
        worker.execute(lease)
    assert raised.value is error
    snapshot = jobs.snapshot(lease.job_id)
    assert snapshot.state is JobState.PARKED
    assert snapshot.attempt == 1
    assert snapshot.park_reason == reason
    assert snapshot.last_error is None
    assert "secret" not in repr(snapshot)

    clock.value = 200 * 365 * 24 * 60 * 60 * 1_000_000_000
    assert jobs.claim((JobType.EPHEMERIS_RETRIEVAL,), "later", 10.0) is None


@pytest.mark.parametrize(
    ("error", "lease_change", "fail_calls", "park_calls"),
    (
        (
            RetryableProviderError(EphemerisSource.HUGGING_FACE, "transient"),
            {"lease_token": "lease_stale"},
            1,
            0,
        ),
        (
            ProviderResponseError(EphemerisSource.HUGGING_FACE, "invalid"),
            {"lease_generation": 2},
            0,
            1,
        ),
    ),
)
def test_stale_failure_mutation_is_fail_closed_and_attempted_only_once(
    error: Exception,
    lease_change: dict[str, object],
    fail_calls: int,
    park_calls: int,
) -> None:
    clock = Clock()
    underlying = InMemoryJobLeaseRepository(now_utc_ns=clock)
    jobs = MutationCountingRepository(underlying)
    lease = leased_job(underlying)
    stale_lease = replace(lease, **lease_change)
    worker = EphemerisRetrievalWorker(
        Preparer(error),
        FakeCommitter(jobs, InMemoryEphemerisSnapshotCatalog()),
        jobs,
        EphemerisRetryPolicy(),
        now_utc_ns=clock,
    )

    with pytest.raises(StaleLeaseError) as raised:
        worker.execute(stale_lease)
    assert raised.value.__context__ is error
    assert jobs.fail_calls == fail_calls
    assert jobs.park_calls == park_calls
    assert underlying.snapshot(lease.job_id).state is JobState.LEASED


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
    snapshot = jobs.snapshot(JobId("job_one"))
    assert snapshot.state is JobState.PARKED
    assert snapshot.park_reason == "processing_failure"
    assert snapshot.last_error is None
    assert preparer.requests == []
