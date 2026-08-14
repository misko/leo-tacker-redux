"""Fenced execution of provider-neutral ephemeris retrieval jobs."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    EphemerisRetrievalId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.ephemeris import (
    EphemerisRetrievalRequest,
    EphemerisSnapshot,
    EphemerisSource,
)
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.jobs.ports import JobLeaseRepository

from .catalog import ArchivedEphemerisSnapshot
from .scheduling import EphemerisRetryPolicy, FailureDisposition


class EphemerisPreparer(Protocol):
    def prepare(
        self, request: EphemerisRetrievalRequest
    ) -> ArchivedEphemerisSnapshot: ...


class FencedEphemerisCommitter(Protocol):
    def publish_and_complete(
        self,
        lease: JobLease,
        archived: ArchivedEphemerisSnapshot,
        result_ref: ArtifactRef,
    ) -> None: ...


class EphemerisRetrievalWorker:
    """Prepare outside the transaction, then fence visibility and completion."""

    def __init__(
        self,
        preparer: EphemerisPreparer,
        committer: FencedEphemerisCommitter,
        jobs: JobLeaseRepository,
        retry_policy: EphemerisRetryPolicy,
        *,
        now_utc_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._preparer = preparer
        self._committer = committer
        self._jobs = jobs
        self._retry_policy = retry_policy
        self._now = now_utc_ns

    def execute(self, lease: JobLease) -> ArchivedEphemerisSnapshot:
        try:
            request = _request_from_lease(lease)
            archived = self._preparer.prepare(request)
        except Exception as error:
            decision = self._retry_policy.decide(
                error,
                attempt=lease.attempt,
                now_utc_ns=UtcNs(self._now()),
            )
            # Store only a bounded policy reason code, never exception text or
            # provider credentials. Terminal dispositions are parked rather
            # than assigned an artificial retry timestamp.
            if decision.disposition is FailureDisposition.RETRY:
                retry_at_utc_ns = decision.retry_at_utc_ns
                if retry_at_utc_ns is None:
                    raise RuntimeError("retry decision requires an exact retry time")
                self._jobs.fail(
                    lease.job_id,
                    lease.lease_token,
                    lease.lease_generation,
                    decision.reason_code,
                    retry_at_utc_ns,
                )
            else:
                self._jobs.park(
                    lease.job_id,
                    lease.lease_token,
                    lease.lease_generation,
                    decision.reason_code,
                )
            raise
        result_ref = ArtifactRef(
            str(archived.snapshot.snapshot_id),
            archived.provenance_object_ref.digest,
            SchemaRef(EphemerisSnapshot.SCHEMA_ID, V0_1),
        )
        self._committer.publish_and_complete(lease, archived, result_ref)
        return archived


def _request_from_lease(lease: JobLease) -> EphemerisRetrievalRequest:
    if lease.job_type is not JobType.EPHEMERIS_RETRIEVAL:
        raise ValueError("worker accepts only ephemeris retrieval jobs")
    expected_schema = SchemaRef("org.leo-flow.ephemeris-retrieval-job", V0_1)
    if lease.payload.schema != expected_schema:
        raise ValueError("unsupported ephemeris retrieval job payload schema")
    payload = dict(lease.payload.value)
    required = {"retrieval_id", "source", "scope", "request_spec", "slot_utc_ns"}
    if set(payload) != required:
        raise ValueError("ephemeris retrieval payload fields are invalid")
    retrieval_id = payload["retrieval_id"]
    source = payload["source"]
    scope = payload["scope"]
    request_spec = payload["request_spec"]
    slot = payload["slot_utc_ns"]
    if (
        not all(
            isinstance(value, str)
            for value in (retrieval_id, source, scope, request_spec)
        )
        or isinstance(slot, bool)
        or not isinstance(slot, int)
    ):
        raise ValueError("ephemeris retrieval payload types are invalid")
    return EphemerisRetrievalRequest(
        EphemerisRetrievalId(retrieval_id),
        EphemerisSource(source),
        scope,
        request_spec,
    )
