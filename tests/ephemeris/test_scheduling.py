from __future__ import annotations

import pytest

from leo_flow.analysis.ephemeris.providers import (
    AuthenticationProviderError,
    ProviderResponseError,
    RetryableProviderError,
)
from leo_flow.analysis.ephemeris.scheduling import (
    EphemerisRetryPolicy,
    EphemerisSchedule,
    EphemerisScheduler,
    FailureDisposition,
)
from leo_flow.contracts.core import UtcNs
from leo_flow.contracts.ephemeris import EphemerisSource
from leo_flow.jobs.memory import InMemoryJobLeaseRepository


def schedule() -> EphemerisSchedule:
    return EphemerisSchedule(
        EphemerisSource.SPACE_TRACK,
        "starlink",
        "gp-starlink-active-v1",
        60,
        UtcNs(1_000_000_000_000),
    )


def test_due_slots_have_stable_identities_and_queue_idempotently() -> None:
    jobs = InMemoryJobLeaseRepository(now_utc_ns=lambda: 0)
    scheduler = EphemerisScheduler(jobs)
    first = scheduler.enqueue_due(
        schedule(),
        after_utc_ns=UtcNs(999_000_000_000),
        now_utc_ns=UtcNs(1_120_000_000_000),
    )
    second = scheduler.enqueue_due(
        schedule(),
        after_utc_ns=UtcNs(999_000_000_000),
        now_utc_ns=UtcNs(1_120_000_000_000),
    )
    assert first == second
    assert [int(item.slot_utc_ns) for item in first] == [
        1_000_000_000_000,
        1_060_000_000_000,
        1_120_000_000_000,
    ]
    assert len({item.job_id for item in first}) == 3
    assert len({item.request.retrieval_id for item in first}) == 3


def test_schedule_bounds_catchup_and_rejects_off_cadence() -> None:
    scheduler = EphemerisScheduler(InMemoryJobLeaseRepository(now_utc_ns=lambda: 0))
    due = scheduler.enqueue_due(
        schedule(),
        after_utc_ns=UtcNs(0),
        now_utc_ns=UtcNs(2_000_000_000_000),
        maximum_slots=2,
    )
    assert len(due) == 2
    with pytest.raises(ValueError, match="cadence"):
        scheduler.enqueue_slot(schedule(), UtcNs(1_000_000_000_001))


def test_changed_schedule_cannot_reuse_job_identity() -> None:
    jobs = InMemoryJobLeaseRepository(now_utc_ns=lambda: 0)
    scheduler = EphemerisScheduler(jobs)
    slot = UtcNs(1_000_000_000_000)
    original = scheduler.enqueue_slot(schedule(), slot)
    changed = EphemerisSchedule(
        schedule().source, schedule().scope, "another-query", 60, schedule().anchor_utc_ns
    )
    other = scheduler.enqueue_slot(changed, slot)
    assert original.job_id != other.job_id


def test_retry_policy_honors_rate_limit_and_classifies_auth() -> None:
    policy = EphemerisRetryPolicy(initial_delay_s=10, maximum_delay_s=60)
    now = UtcNs(1_000_000_000)
    rate = policy.decide(
        RetryableProviderError(EphemerisSource.SPACE_TRACK, "limited", 37),
        attempt=5,
        now_utc_ns=now,
    )
    assert rate.disposition is FailureDisposition.RETRY
    assert rate.retry_at_utc_ns == UtcNs(38_000_000_000)
    assert rate.reason_code == "provider_rate_limit"

    transient = policy.decide(
        RetryableProviderError(EphemerisSource.SPACE_TRACK, "server"),
        attempt=4,
        now_utc_ns=now,
    )
    assert transient.retry_at_utc_ns == UtcNs(61_000_000_000)

    auth = policy.decide(
        AuthenticationProviderError(EphemerisSource.SPACE_TRACK, "rejected"),
        attempt=1,
        now_utc_ns=now,
    )
    assert auth.disposition is FailureDisposition.OPERATOR_ACTION
    assert auth.retry_at_utc_ns is None

    invalid = policy.decide(
        ProviderResponseError(EphemerisSource.SPACE_TRACK, "bad body"),
        attempt=1,
        now_utc_ns=now,
    )
    assert invalid.disposition is FailureDisposition.PERMANENT
