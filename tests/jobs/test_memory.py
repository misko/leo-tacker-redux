from __future__ import annotations

import threading

import pytest

from leo_flow.contracts.core import ArtifactRef, JobId, SchemaRef, UtcNs
from leo_flow.jobs import (
    InMemoryJobLeaseRepository,
    JobPayload,
    JobState,
    JobType,
    StaleLeaseError,
)
from testkit import digest


class Clock:
    def __init__(self):
        self.now = 1_700_000_000_000_000_000

    def __call__(self):
        return self.now


def repository():
    clock = Clock()
    counter = iter(range(100))
    repo = InMemoryJobLeaseRepository(
        now_utc_ns=clock, token_factory=lambda: f"lease_{next(counter)}"
    )
    payload = JobPayload.create(
        SchemaRef("org.leo-flow.job.test"), {"recording_id": "rec_01"}
    )
    repo.enqueue(JobId("job_01"), JobType.RECORDING_ANALYSIS, payload)
    return repo, clock


def test_stale_generation_cannot_complete_after_reclaim() -> None:
    repo, clock = repository()
    first = repo.claim((JobType.RECORDING_ANALYSIS,), "worker-a", 1.0)
    assert first is not None
    clock.now = int(first.lease_expires_utc_ns)
    second = repo.claim((JobType.RECORDING_ANALYSIS,), "worker-b", 1.0)
    assert second is not None and second.lease_generation == first.lease_generation + 1
    result = ArtifactRef("result_01", digest("result"))
    with pytest.raises(StaleLeaseError):
        repo.complete(first.job_id, first.lease_token, first.lease_generation, result)
    repo.complete(second.job_id, second.lease_token, second.lease_generation, result)
    assert repo.snapshot(second.job_id).state is JobState.SUCCEEDED


def test_expired_heartbeat_and_completion_are_rejected() -> None:
    repo, clock = repository()
    lease = repo.claim((JobType.RECORDING_ANALYSIS,), "worker", 1.0)
    assert lease is not None
    clock.now = int(lease.lease_expires_utc_ns)
    with pytest.raises(StaleLeaseError):
        repo.heartbeat(lease.job_id, lease.lease_token, lease.lease_generation, 1.0)
    with pytest.raises(StaleLeaseError):
        repo.complete(
            lease.job_id,
            lease.lease_token,
            lease.lease_generation,
            ArtifactRef("result_01", digest()),
        )


def test_fail_requeues_at_declared_time() -> None:
    repo, clock = repository()
    lease = repo.claim((JobType.RECORDING_ANALYSIS,), "worker", 1.0)
    assert lease is not None
    retry = UtcNs(clock.now + 5_000_000_000)
    repo.fail(
        lease.job_id, lease.lease_token, lease.lease_generation, "transient", retry
    )
    assert repo.claim((JobType.RECORDING_ANALYSIS,), "worker", 1.0) is None
    clock.now = int(retry)
    assert repo.claim((JobType.RECORDING_ANALYSIS,), "worker", 1.0) is not None


def test_concurrent_claim_exposes_only_one_lease() -> None:
    repo, _clock = repository()
    leases = []
    lock = threading.Lock()

    def claim():
        lease = repo.claim((JobType.RECORDING_ANALYSIS,), "worker", 5.0)
        if lease:
            with lock:
                leases.append(lease)

    threads = [threading.Thread(target=claim) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(leases) == 1


def test_park_is_terminal_inspectable_and_never_claimable() -> None:
    repo, clock = repository()
    lease = repo.claim((JobType.RECORDING_ANALYSIS,), "worker", 1.0)
    assert lease is not None

    repo.park(
        lease.job_id,
        lease.lease_token,
        lease.lease_generation,
        "operator_action_required",
    )
    first = repo.snapshot(lease.job_id)
    assert first == repo.snapshot(lease.job_id)
    assert first.state is JobState.PARKED
    assert first.park_reason == "operator_action_required"
    assert first.parked_at_utc_ns == UtcNs(clock.now)
    assert first.result_ref is None
    assert first.last_error is None

    clock.now += 100 * 365 * 24 * 60 * 60 * 1_000_000_000
    assert repo.claim((JobType.RECORDING_ANALYSIS,), "later", 1.0) is None


def test_park_rejects_stale_generation_and_expired_lease() -> None:
    repo, clock = repository()
    first = repo.claim((JobType.RECORDING_ANALYSIS,), "first", 1.0)
    assert first is not None
    clock.now = int(first.lease_expires_utc_ns)
    second = repo.claim((JobType.RECORDING_ANALYSIS,), "second", 1.0)
    assert second is not None

    with pytest.raises(StaleLeaseError):
        repo.park(
            first.job_id,
            first.lease_token,
            first.lease_generation,
            "stale_generation",
        )
    clock.now = int(second.lease_expires_utc_ns)
    with pytest.raises(StaleLeaseError):
        repo.park(
            second.job_id,
            second.lease_token,
            second.lease_generation,
            "expired_lease",
        )


@pytest.mark.parametrize(
    "reason",
    ["", "Contains Spaces", "UPPERCASE", "x" * 129, "exception\nsecret"],
)
def test_park_requires_bounded_sanitized_reason(reason: str) -> None:
    repo, _clock = repository()
    lease = repo.claim((JobType.RECORDING_ANALYSIS,), "worker", 1.0)
    assert lease is not None

    with pytest.raises(ValueError, match="parking reason"):
        repo.park(lease.job_id, lease.lease_token, lease.lease_generation, reason)
    assert repo.snapshot(lease.job_id).state is JobState.LEASED


def test_fail_none_legacy_semantics_remain_immediately_retryable() -> None:
    repo, _clock = repository()
    lease = repo.claim((JobType.RECORDING_ANALYSIS,), "worker", 1.0)
    assert lease is not None
    repo.fail(
        lease.job_id,
        lease.lease_token,
        lease.lease_generation,
        "legacy_failure",
        None,
    )

    retried = repo.claim((JobType.RECORDING_ANALYSIS,), "retry", 1.0)
    assert retried is not None
    assert retried.attempt == 2
