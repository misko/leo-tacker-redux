"""Deterministic ephemeris job scheduling and retry decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from leo_flow.contracts.core import (
    Digest,
    EphemerisRetrievalId,
    JobId,
    SchemaRef,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.ephemeris import (
    EphemerisRetrievalRequest,
    EphemerisSource,
)
from leo_flow.jobs.contracts import JobPayload, JobType

from .providers import (
    AuthenticationProviderError,
    ProviderResponseError,
    RetryableProviderError,
)


class JobEnqueuer(Protocol):
    def enqueue(
        self,
        job_id: JobId,
        job_type: JobType,
        payload: JobPayload,
        *,
        available_at_utc_ns: UtcNs | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class EphemerisSchedule:
    source: EphemerisSource
    scope: str
    request_spec: str
    cadence_s: int
    anchor_utc_ns: UtcNs

    def __post_init__(self) -> None:
        if self.cadence_s <= 0:
            raise ValueError("cadence must be positive")
        if not self.scope or not self.request_spec:
            raise ValueError("scope and request spec are required")


@dataclass(frozen=True)
class ScheduledRetrieval:
    slot_utc_ns: UtcNs
    job_id: JobId
    request: EphemerisRetrievalRequest


class EphemerisScheduler:
    """Map clock slots to stable job/retrieval IDs for at-least-once queues."""

    PAYLOAD_SCHEMA = SchemaRef("org.leo-flow.ephemeris-retrieval-job")

    def __init__(self, jobs: JobEnqueuer) -> None:
        self._jobs = jobs

    def enqueue_slot(
        self, schedule: EphemerisSchedule, slot_utc_ns: UtcNs
    ) -> ScheduledRetrieval:
        cadence_ns = schedule.cadence_s * 1_000_000_000
        offset = int(slot_utc_ns) - int(schedule.anchor_utc_ns)
        if offset < 0 or offset % cadence_ns:
            raise ValueError("slot is not on the schedule cadence")
        token = Digest.sha256(
            canonical_json_bytes(
                {
                    "source": schedule.source.value,
                    "scope": schedule.scope,
                    "request_spec": schedule.request_spec,
                    "slot_utc_ns": int(slot_utc_ns),
                }
            )
        ).value
        retrieval_id = EphemerisRetrievalId(f"ephret_{token}")
        job_id = JobId(f"job_{token}")
        request = EphemerisRetrievalRequest(
            retrieval_id,
            schedule.source,
            schedule.scope,
            schedule.request_spec,
        )
        payload = JobPayload.create(
            self.PAYLOAD_SCHEMA,
            {
                "retrieval_id": str(retrieval_id),
                "source": schedule.source.value,
                "scope": schedule.scope,
                "request_spec": schedule.request_spec,
                "slot_utc_ns": int(slot_utc_ns),
            },
        )
        self._jobs.enqueue(
            job_id,
            JobType.EPHEMERIS_RETRIEVAL,
            payload,
            available_at_utc_ns=slot_utc_ns,
        )
        return ScheduledRetrieval(slot_utc_ns, job_id, request)

    def enqueue_due(
        self,
        schedule: EphemerisSchedule,
        *,
        after_utc_ns: UtcNs,
        now_utc_ns: UtcNs,
        maximum_slots: int = 24,
    ) -> tuple[ScheduledRetrieval, ...]:
        if maximum_slots <= 0:
            raise ValueError("maximum slots must be positive")
        cadence_ns = schedule.cadence_s * 1_000_000_000
        anchor = int(schedule.anchor_utc_ns)
        first_index = max(0, (int(after_utc_ns) - anchor) // cadence_ns + 1)
        last_index = (int(now_utc_ns) - anchor) // cadence_ns
        if last_index < first_index:
            return ()
        indices = range(first_index, min(last_index + 1, first_index + maximum_slots))
        return tuple(
            self.enqueue_slot(schedule, UtcNs(anchor + index * cadence_ns))
            for index in indices
        )


class FailureDisposition(str, Enum):
    RETRY = "retry"
    OPERATOR_ACTION = "operator_action"
    PERMANENT = "permanent"


@dataclass(frozen=True)
class RetryDecision:
    disposition: FailureDisposition
    retry_at_utc_ns: UtcNs | None
    reason_code: str


@dataclass(frozen=True)
class EphemerisRetryPolicy:
    initial_delay_s: int = 60
    maximum_delay_s: int = 3600

    def __post_init__(self) -> None:
        if not 0 < self.initial_delay_s <= self.maximum_delay_s:
            raise ValueError("retry delay bounds are invalid")

    def decide(
        self, error: Exception, *, attempt: int, now_utc_ns: UtcNs
    ) -> RetryDecision:
        if attempt <= 0:
            raise ValueError("attempt must be positive")
        if isinstance(error, AuthenticationProviderError):
            return RetryDecision(
                FailureDisposition.OPERATOR_ACTION, None, "provider_authentication"
            )
        if isinstance(error, RetryableProviderError):
            delay = error.retry_after_s
            if delay is None:
                delay = min(
                    self.maximum_delay_s,
                    self.initial_delay_s * (2 ** min(attempt - 1, 30)),
                )
            return RetryDecision(
                FailureDisposition.RETRY,
                UtcNs(int(now_utc_ns) + delay * 1_000_000_000),
                "provider_rate_limit"
                if error.retry_after_s is not None
                else "provider_transient",
            )
        if isinstance(error, ProviderResponseError):
            return RetryDecision(
                FailureDisposition.PERMANENT, None, "provider_response_invalid"
            )
        return RetryDecision(FailureDisposition.PERMANENT, None, "processing_failure")
