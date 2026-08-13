"""Correct fenced lease semantics for component tests and local composition."""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from leo_flow.contracts.core import ArtifactRef, JobId, UtcNs

from .contracts import JobLease, JobPayload, JobType
from .ports import StaleLeaseError


class LeaseError(RuntimeError):
    pass


class JobConflictError(LeaseError):
    pass


class JobState(str, Enum):
    READY = "ready"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class JobSnapshot:
    job_id: JobId
    state: JobState
    attempt: int
    lease_generation: int
    result_ref: ArtifactRef | None
    last_error: str | None


@dataclass
class _JobRecord:
    job_id: JobId
    job_type: JobType
    payload: JobPayload
    state: JobState
    available_at_utc_ns: int
    attempt: int = 0
    lease_token: str | None = None
    lease_generation: int = 0
    lease_expires_utc_ns: int | None = None
    result_ref: ArtifactRef | None = None
    last_error: str | None = None


class InMemoryJobLeaseRepository:
    """Thread-safe semantic fake, not evidence of database-level correctness."""

    def __init__(
        self,
        *,
        now_utc_ns: Callable[[], int] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._now = now_utc_ns or time.time_ns
        self._token = token_factory or (lambda: f"lease_{secrets.token_hex(16)}")
        self._lock = threading.RLock()
        self._jobs: dict[JobId, _JobRecord] = {}

    def enqueue(
        self,
        job_id: JobId,
        job_type: JobType,
        payload: JobPayload,
        *,
        available_at_utc_ns: UtcNs | None = None,
    ) -> None:
        with self._lock:
            existing = self._jobs.get(job_id)
            if existing is not None:
                if existing.job_type != job_type or existing.payload != payload:
                    raise JobConflictError("job ID already has a different payload")
                return
            self._jobs[job_id] = _JobRecord(
                job_id,
                job_type,
                payload,
                JobState.READY,
                int(
                    available_at_utc_ns
                    if available_at_utc_ns is not None
                    else self._now()
                ),
            )

    def claim(
        self, types: tuple[JobType, ...], worker_id: str, ttl_s: float
    ) -> JobLease | None:
        del worker_id  # Ownership is represented by the unguessable lease token.
        ttl_ns = self._ttl_ns(ttl_s)
        now = self._now()
        with self._lock:
            eligible = [
                record
                for record in self._jobs.values()
                if record.job_type in types
                and record.state is not JobState.SUCCEEDED
                and record.available_at_utc_ns <= now
                and (
                    record.state in (JobState.READY, JobState.FAILED)
                    or (
                        record.state is JobState.LEASED
                        and record.lease_expires_utc_ns is not None
                        and record.lease_expires_utc_ns <= now
                    )
                )
            ]
            if not eligible:
                return None
            record = min(eligible, key=lambda item: str(item.job_id))
            record.state = JobState.LEASED
            record.attempt += 1
            record.lease_generation += 1
            record.lease_token = self._token()
            record.lease_expires_utc_ns = now + ttl_ns
            return self._lease(record)

    def heartbeat(
        self, job_id: JobId, lease_token: str, generation: int, ttl_s: float
    ) -> JobLease:
        ttl_ns = self._ttl_ns(ttl_s)
        now = self._now()
        with self._lock:
            record = self._require_active(job_id, lease_token, generation, now)
            record.lease_expires_utc_ns = now + ttl_ns
            return self._lease(record)

    def complete(
        self,
        job_id: JobId,
        lease_token: str,
        generation: int,
        result_ref: ArtifactRef,
    ) -> None:
        with self._lock:
            record = self._require_active(job_id, lease_token, generation, self._now())
            record.state = JobState.SUCCEEDED
            record.result_ref = result_ref
            record.lease_token = None
            record.lease_expires_utc_ns = None

    def fail(
        self,
        job_id: JobId,
        lease_token: str,
        generation: int,
        reason: str,
        retry_at_utc_ns: UtcNs | None,
    ) -> None:
        if not reason:
            raise ValueError("failure reason cannot be empty")
        with self._lock:
            record = self._require_active(job_id, lease_token, generation, self._now())
            record.state = JobState.FAILED
            record.last_error = reason
            record.available_at_utc_ns = int(
                retry_at_utc_ns if retry_at_utc_ns is not None else self._now()
            )
            record.lease_token = None
            record.lease_expires_utc_ns = None

    def snapshot(self, job_id: JobId) -> JobSnapshot:
        with self._lock:
            record = self._jobs[job_id]
            return JobSnapshot(
                record.job_id,
                record.state,
                record.attempt,
                record.lease_generation,
                record.result_ref,
                record.last_error,
            )

    def _require_active(
        self, job_id: JobId, token: str, generation: int, now: int
    ) -> _JobRecord:
        try:
            record = self._jobs[job_id]
        except KeyError as error:
            raise StaleLeaseError("unknown job") from error
        if (
            record.state is not JobState.LEASED
            or record.lease_token != token
            or record.lease_generation != generation
            or record.lease_expires_utc_ns is None
            or record.lease_expires_utc_ns <= now
        ):
            raise StaleLeaseError("lease token, generation, or expiry is stale")
        return record

    @staticmethod
    def _ttl_ns(ttl_s: float) -> int:
        if ttl_s <= 0:
            raise ValueError("lease TTL must be positive")
        return max(1, int(ttl_s * 1_000_000_000))

    @staticmethod
    def _lease(record: _JobRecord) -> JobLease:
        assert record.lease_token is not None
        assert record.lease_expires_utc_ns is not None
        return JobLease(
            record.job_id,
            record.job_type,
            record.payload,
            record.attempt,
            record.lease_token,
            record.lease_generation,
            UtcNs(record.lease_expires_utc_ns),
        )
