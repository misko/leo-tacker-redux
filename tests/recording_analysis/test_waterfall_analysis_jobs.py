from __future__ import annotations

import math
import struct
from dataclasses import replace

import pytest

from leo_flow.analysis.recording import (
    BoundedWaterfallAnalyzerV0_1,
    WaterfallConfigV0_1,
    waterfall_algorithm_ref_v0_1,
    waterfall_config_ref_v0_1,
)
from leo_flow.contracts.core import ArtifactRef, JobId, SchemaRef, UtcNs
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.contracts.waterfall import WaterfallBundleV0_1
from leo_flow.jobs import InMemoryJobLeaseRepository, JobPayload, JobState, JobType
from leo_flow.services.waterfall_analysis import (
    FencedWaterfallAnalysisWorkerV0_1,
    PreparedWaterfallAnalysisV0_1,
    WaterfallAnalysisJobError,
    WaterfallAnalysisJobPreparerV0_1,
    decode_waterfall_analysis_payload,
    waterfall_analysis_payload,
)
from leo_flow.services.waterfall_submission import (
    WaterfallAnalysisSubmissionServiceV0_1,
    WaterfallAnalysisSubmissionV0_1,
)

from .fakes import SegmentFixture, execution_context, make_view


def _fixture():
    samples: list[int] = []
    for index in range(32):
        phase = 2 * math.pi * index / 8
        samples.extend(
            (
                round(1000 * math.cos(phase)),
                round(1000 * math.sin(phase)),
                round(500 * math.cos(phase)),
                round(500 * math.sin(phase)),
            )
        )
    view, recording_ref = make_view(
        SegmentFixture(struct.pack(f"<{len(samples)}h", *samples), 16_000)
    )
    config = WaterfallConfigV0_1(fft_window_samples=16, frequency_bins=8)
    submitted = WaterfallAnalysisSubmissionServiceV0_1(
        InMemoryJobLeaseRepository()
    ).submit(
        WaterfallAnalysisSubmissionV0_1(
            PublishedRecordingRef(recording_ref),
            waterfall_algorithm_ref_v0_1(),
            waterfall_config_ref_v0_1(config),
            (),
        )
    )
    bundle = BoundedWaterfallAnalyzerV0_1(
        config, execution_context()
    ).analyze_waterfall(view, submitted.request)
    return view, submitted.request, bundle


class _Context:
    def __init__(self, value: object) -> None:
        self._value = value

    def __enter__(self) -> object:
        return self._value

    def __exit__(self, *_args: object) -> None:
        return None


class _Reader:
    def __init__(self, expected: object, view: object) -> None:
        self.expected = expected
        self.view = view
        self.calls = 0

    def open(self, ref: object) -> _Context:
        assert ref == self.expected
        self.calls += 1
        return _Context(self.view)


class _Analyzer:
    def __init__(self, expected: object, bundle: WaterfallBundleV0_1) -> None:
        self.expected = expected
        self.bundle = bundle

    def analyze_waterfall(self, recording: object, request: object):
        assert recording == "recording-view"
        assert request == self.expected
        return self.bundle


class _Committer:
    def __init__(self, jobs: InMemoryJobLeaseRepository) -> None:
        self.jobs = jobs

    def commit_waterfall(self, lease, prepared: PreparedWaterfallAnalysisV0_1):
        result = ArtifactRef(
            str(prepared.bundle.product_id),
            prepared.bundle.input_recording_identity_digest,
            prepared.bundle.schema,
        )
        self.jobs.complete(
            lease.job_id, lease.lease_token, lease.lease_generation, result
        )
        return result


def test_waterfall_submission_and_payload_are_exact_and_idempotent() -> None:
    _, request, _ = _fixture()
    jobs = InMemoryJobLeaseRepository()
    service = WaterfallAnalysisSubmissionServiceV0_1(jobs)
    selection = WaterfallAnalysisSubmissionV0_1(
        PublishedRecordingRef(request.recording_object_ref),
        request.algorithm_ref,
        request.config_ref,
        request.dependency_refs,
    )

    first = service.submit(selection)
    second = service.submit(selection)

    assert first == second
    assert decode_waterfall_analysis_payload(first.payload) == request
    assert jobs.snapshot(first.job_id).state is JobState.READY
    with pytest.raises(WaterfallAnalysisJobError, match="schema"):
        decode_waterfall_analysis_payload(
            replace(first.payload, schema=SchemaRef("wrong"))
        )
    with pytest.raises(WaterfallAnalysisJobError, match="fields"):
        decode_waterfall_analysis_payload(
            JobPayload.create(first.payload.schema, {"recording_id": "rec_partial"})
        )


def test_worker_claims_only_waterfall_and_completes() -> None:
    _, request, bundle = _fixture()
    jobs = InMemoryJobLeaseRepository(
        now_utc_ns=lambda: 100, token_factory=lambda: "lease_waterfall"
    )
    jobs.enqueue(
        JobId("job_feature_waits"),
        JobType.RECORDING_ANALYSIS,
        JobPayload.create(SchemaRef("feature-job"), {}),
        available_at_utc_ns=UtcNs(0),
    )
    jobs.enqueue(
        JobId("job_waterfall_runs"),
        JobType.WATERFALL_ANALYSIS,
        waterfall_analysis_payload(request),
        available_at_utc_ns=UtcNs(0),
    )
    worker = FencedWaterfallAnalysisWorkerV0_1(
        jobs,
        WaterfallAnalysisJobPreparerV0_1(
            _Reader(request.recording_object_ref, "recording-view"),
            _Analyzer(request, bundle),
        ),
        _Committer(jobs),
        worker_id="waterfall-worker",
        lease_ttl_s=10,
    )

    assert worker.process_one_job()
    assert jobs.snapshot(JobId("job_waterfall_runs")).state is JobState.SUCCEEDED
    assert jobs.snapshot(JobId("job_feature_waits")).state is JobState.READY
    assert not worker.process_one_job()


def test_worker_retries_then_parks_bounded_failures() -> None:
    _, request, bundle = _fixture()
    jobs = InMemoryJobLeaseRepository(
        now_utc_ns=lambda: 100, token_factory=lambda: "lease_waterfall"
    )
    job_id = JobId("job_waterfall_fails")
    jobs.enqueue(
        job_id,
        JobType.WATERFALL_ANALYSIS,
        waterfall_analysis_payload(request),
        available_at_utc_ns=UtcNs(0),
    )

    class _FailingCommitter:
        def commit_waterfall(self, lease, prepared):
            raise OSError("transient details must not cross the worker boundary")

    worker = FencedWaterfallAnalysisWorkerV0_1(
        jobs,
        WaterfallAnalysisJobPreparerV0_1(
            _Reader(request.recording_object_ref, "recording-view"),
            _Analyzer(request, bundle),
        ),
        _FailingCommitter(),
        worker_id="waterfall-worker",
        lease_ttl_s=10,
        maximum_attempts=1,
    )

    assert worker.process_one_job()
    parked = jobs.snapshot(job_id)
    assert parked.state is JobState.PARKED
    assert parked.park_reason == "waterfall-attempts-exhausted"
    assert parked.last_error is None
