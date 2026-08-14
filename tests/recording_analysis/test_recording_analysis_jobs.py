from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.contracts.core import ArtifactRef, JobId, SchemaRef, UtcNs
from leo_flow.jobs import InMemoryJobLeaseRepository, JobPayload, JobType
from leo_flow.jobs.memory import JobState
from leo_flow.services.recording_analysis import (
    FencedRecordingAnalysisWorker,
    PreparedRecordingAnalysis,
    RecordingAnalysisJobError,
    RecordingAnalysisJobPreparer,
    decode_recording_analysis_payload,
    recording_analysis_payload,
)
from tests.recording_analysis.test_feature_persistence import _fixture


class _ViewContext:
    def __init__(self, value: object) -> None:
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *args):
        return None


class _Reader:
    def __init__(self, expected, view: object) -> None:
        self.expected = expected
        self.view = view
        self.calls = []

    def open(self, ref):
        assert ref == self.expected
        self.calls.append(ref)
        return _ViewContext(self.view)


class _Analyzer:
    def __init__(self, expected_request, bundle) -> None:
        self.expected_request = expected_request
        self.bundle = bundle

    def analyze(self, recording, request):
        assert recording == "recording-view"
        assert request == self.expected_request
        return self.bundle


def test_recording_analysis_payload_round_trips_strictly() -> None:
    request, _ = _fixture()
    payload = recording_analysis_payload(request)
    assert decode_recording_analysis_payload(payload) == request
    with pytest.raises(RecordingAnalysisJobError, match="schema"):
        decode_recording_analysis_payload(replace(payload, schema=SchemaRef("wrong")))
    with pytest.raises(RecordingAnalysisJobError, match="fields"):
        decode_recording_analysis_payload(
            JobPayload.create(payload.schema, {"recording_id": "rec_incomplete"})
        )


def test_preparer_rejects_other_job_types_before_opening_recording() -> None:
    request, bundle = _fixture()
    reader = _Reader(request.recording_object_ref, "recording-view")
    preparer = RecordingAnalysisJobPreparer(reader, _Analyzer(request, bundle))
    lease = _lease(JobType.MODEL_ANALYSIS, recording_analysis_payload(request))

    with pytest.raises(RecordingAnalysisJobError, match="only"):
        preparer.prepare(lease)
    assert reader.calls == []


def test_preparer_decodes_opens_and_analyzes_outside_committer() -> None:
    request, bundle = _fixture()
    reader = _Reader(request.recording_object_ref, "recording-view")
    preparer = RecordingAnalysisJobPreparer(reader, _Analyzer(request, bundle))
    lease = _lease(JobType.RECORDING_ANALYSIS, recording_analysis_payload(request))

    assert preparer.prepare(lease) == PreparedRecordingAnalysis(request, bundle)
    assert reader.calls == [request.recording_object_ref]


class _Committer:
    def __init__(self, jobs: InMemoryJobLeaseRepository) -> None:
        self.jobs = jobs

    def commit(self, lease, prepared) -> ArtifactRef:
        result = ArtifactRef(
            str(prepared.bundle.feature_set_id),
            prepared.bundle.input_recording_identity_digest,
            prepared.bundle.schema,
        )
        self.jobs.complete(
            lease.job_id, lease.lease_token, lease.lease_generation, result
        )
        return result


def test_typed_worker_claims_only_recording_analysis() -> None:
    request, bundle = _fixture()
    jobs = InMemoryJobLeaseRepository(
        now_utc_ns=lambda: 100, token_factory=lambda: "lease_recording"
    )
    jobs.enqueue(
        JobId("job_model_waits"),
        JobType.MODEL_ANALYSIS,
        JobPayload.create(SchemaRef("model-job"), {}),
        available_at_utc_ns=UtcNs(0),
    )
    jobs.enqueue(
        JobId("job_recording_runs"),
        JobType.RECORDING_ANALYSIS,
        recording_analysis_payload(request),
        available_at_utc_ns=UtcNs(0),
    )
    worker = FencedRecordingAnalysisWorker(
        jobs,
        RecordingAnalysisJobPreparer(
            _Reader(request.recording_object_ref, "recording-view"),
            _Analyzer(request, bundle),
        ),
        _Committer(jobs),
        worker_id="recording-worker",
        lease_ttl_s=10,
    )

    assert worker.process_one_job()
    assert jobs.snapshot(JobId("job_recording_runs")).state is JobState.SUCCEEDED
    assert jobs.snapshot(JobId("job_model_waits")).state is JobState.READY
    assert not worker.process_one_job()


def _lease(job_type: JobType, payload: JobPayload):
    from leo_flow.jobs.contracts import JobLease

    return JobLease(
        JobId("job_fixture"), job_type, payload, 1, "lease_fixture", 1, UtcNs(1000)
    )
