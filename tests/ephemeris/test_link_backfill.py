from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from leo_flow.analysis.ephemeris.backfill import (
    EphemerisLinkBackfillError,
    EphemerisLinkBackfillExecutor,
    EphemerisLinkBackfillPreparer,
    EphemerisLinkRequest,
    decode_ephemeris_link_payload,
    ephemeris_link_payload,
)
from leo_flow.contracts.core import ArtifactRef, JobId, RecordingId, SchemaRef, UtcNs
from leo_flow.contracts.ephemeris import EphemerisSelectionPolicy, EphemerisSource
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)
from leo_flow.jobs import InMemoryJobLeaseRepository, JobType
from leo_flow.jobs.memory import JobState
from testkit import digest


def _request() -> EphemerisLinkRequest:
    return EphemerisLinkRequest(
        RecordingId("rec_link"),
        EphemerisSource.HUGGING_FACE,
        "starlink",
        EphemerisSelectionPolicy.AVAILABLE_THEN,
        ArtifactRef("temporal-v1", digest("temporal"), SchemaRef("org.test.policy")),
        UtcNs(200),
    )


def test_typed_link_payload_round_trips_exact_selection_inputs() -> None:
    request = _request()
    assert decode_ephemeris_link_payload(ephemeris_link_payload(request)) == request


def test_best_ephemeris_and_wrong_schema_fail_closed() -> None:
    request = _request()
    with pytest.raises(ValueError, match="no frozen"):
        replace(request, policy=EphemerisSelectionPolicy.BEST_EPHEMERIS)
    payload = ephemeris_link_payload(request)
    with pytest.raises(EphemerisLinkBackfillError, match="unsupported"):
        decode_ephemeris_link_payload(replace(payload, schema=SchemaRef("wrong")))


class _Context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *args):
        return None


def test_preparer_uses_authoritative_ref_and_digest_verified_manifest() -> None:
    request = _request()
    ref = RecordingObjectRef(
        request.recording_id,
        ObjectRef(digest("data"), 1, "application/octet-stream", "data-v1", "x:data"),
        ObjectRef(digest("meta"), 1, "application/json", "meta-v1", "x:meta"),
        digest("manifest"),
    )
    catalog = SimpleNamespace(get=lambda recording_id: PublishedRecordingRef(ref))
    manifest = SimpleNamespace(
        recording_id=request.recording_id,
        capture_started_utc_ns=UtcNs(100),
        capture_finished_utc_ns=UtcNs(150),
    )
    reader = SimpleNamespace(
        open=lambda exact_ref: _Context(SimpleNamespace(manifest=manifest))
    )
    jobs = InMemoryJobLeaseRepository(
        now_utc_ns=lambda: 1, token_factory=lambda: "token"
    )
    jobs.enqueue(
        JobId("job_link_prepare"),
        JobType.EPHEMERIS_LINK_BACKFILL,
        ephemeris_link_payload(request),
    )
    lease = jobs.claim((JobType.EPHEMERIS_LINK_BACKFILL,), "worker", 5)
    assert lease is not None

    prepared = EphemerisLinkBackfillPreparer(catalog, reader).prepare(lease)
    assert prepared.recording_ref == ref
    assert prepared.recording_interval.started_utc_ns == 100


def test_executor_fences_failure_into_retryable_job_state() -> None:
    request = _request()
    jobs = InMemoryJobLeaseRepository(
        now_utc_ns=lambda: 1, token_factory=lambda: "token"
    )
    jobs.enqueue(
        JobId("job_link_retry"),
        JobType.EPHEMERIS_LINK_BACKFILL,
        ephemeris_link_payload(request),
    )
    lease = jobs.claim((JobType.EPHEMERIS_LINK_BACKFILL,), "worker", 5)
    assert lease is not None
    preparer = SimpleNamespace(
        prepare=lambda claimed: (_ for _ in ()).throw(RuntimeError("missing snapshot"))
    )
    executor = EphemerisLinkBackfillExecutor(
        jobs, preparer, SimpleNamespace(commit=lambda *_: None)
    )
    with pytest.raises(RuntimeError, match="missing snapshot"):
        executor.execute(lease)
    assert jobs.snapshot(lease.job_id).state is JobState.FAILED
