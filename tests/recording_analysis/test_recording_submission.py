from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.contracts.core import ArtifactRef, Digest, SchemaRef
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.jobs import InMemoryJobLeaseRepository, JobState
from leo_flow.services.recording_analysis import decode_recording_analysis_payload
from leo_flow.services.recording_submission import (
    RecordingAnalysisSubmission,
    RecordingAnalysisSubmissionService,
)
from tests.recording_analysis.test_feature_persistence import _fixture


def test_published_recording_is_submitted_as_one_exact_idempotent_job() -> None:
    request, _ = _fixture()
    jobs = InMemoryJobLeaseRepository(now_utc_ns=lambda: 100)
    service = RecordingAnalysisSubmissionService(jobs)
    submission = RecordingAnalysisSubmission(
        PublishedRecordingRef(request.recording_object_ref),
        request.algorithm_ref,
        request.config_ref,
        request.dependency_refs,
        request.requested_output_schema,
    )

    first = service.submit(submission)
    second = service.submit(submission)

    assert first == second
    assert decode_recording_analysis_payload(first.payload) == request
    assert jobs.snapshot(first.job_id).state is JobState.READY


def test_config_or_recording_change_produces_another_job_identity() -> None:
    request, _ = _fixture()
    jobs = InMemoryJobLeaseRepository(now_utc_ns=lambda: 100)
    service = RecordingAnalysisSubmissionService(jobs)
    original = RecordingAnalysisSubmission(
        PublishedRecordingRef(request.recording_object_ref),
        request.algorithm_ref,
        request.config_ref,
        request.dependency_refs,
        request.requested_output_schema,
    )
    changed = replace(
        original,
        config_ref=ArtifactRef("config-other", Digest.sha256(b"config-other")),
    )

    assert service.submit(original).job_id != service.submit(changed).job_id


def test_duplicate_dependency_authority_is_rejected_before_enqueue() -> None:
    request, _ = _fixture()
    dependency = ArtifactRef("same", Digest.sha256(b"one"))
    with pytest.raises(ValueError, match="unique"):
        RecordingAnalysisSubmission(
            PublishedRecordingRef(request.recording_object_ref),
            request.algorithm_ref,
            request.config_ref,
            (dependency, replace(dependency, digest=Digest.sha256(b"two"))),
            request.requested_output_schema,
        )


def test_output_schema_must_be_explicit_and_supported() -> None:
    request, _ = _fixture()
    with pytest.raises(ValueError, match="output schema"):
        RecordingAnalysisSubmission(
            PublishedRecordingRef(request.recording_object_ref),
            request.algorithm_ref,
            request.config_ref,
            request.dependency_refs,
            SchemaRef("org.leo-flow.unsupported-output"),
        )
