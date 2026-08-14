"""Exact, idempotent submission of one published recording for analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts._validation import thaw_value
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    JobId,
    SchemaRef,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.features import FeatureSetBundle, RecordingAnalysisRequest
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.jobs.contracts import JobPayload, JobType

from .recording_analysis import recording_analysis_payload


class RecordingAnalysisJobEnqueuer(Protocol):
    """The only queue capability needed by recording submission."""

    def enqueue(
        self,
        job_id: JobId,
        job_type: JobType,
        payload: JobPayload,
        *,
        available_at_utc_ns: UtcNs | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class RecordingAnalysisSubmission:
    recording: PublishedRecordingRef
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    dependency_refs: tuple[ArtifactRef, ...]
    requested_output_schema: SchemaRef

    def __post_init__(self) -> None:
        if self.requested_output_schema != SchemaRef(FeatureSetBundle.SCHEMA_ID):
            raise ValueError("recording-analysis output schema is unsupported")
        ids = [item.artifact_id for item in self.dependency_refs]
        if len(ids) != len(set(ids)):
            raise ValueError("recording-analysis dependency IDs must be unique")


@dataclass(frozen=True)
class SubmittedRecordingAnalysis:
    job_id: JobId
    payload: JobPayload
    request: RecordingAnalysisRequest


class RecordingAnalysisSubmissionService:
    """Turn an immutable publication into one fully pinned analysis job."""

    def __init__(self, jobs: RecordingAnalysisJobEnqueuer) -> None:
        self._jobs = jobs

    def submit(
        self, submission: RecordingAnalysisSubmission
    ) -> SubmittedRecordingAnalysis:
        recording = submission.recording.recording_object
        dependencies = tuple(
            sorted(
                submission.dependency_refs,
                key=lambda item: (item.artifact_id, str(item.digest)),
            )
        )
        request = RecordingAnalysisRequest(
            schema=SchemaRef(RecordingAnalysisRequest.SCHEMA_ID),
            recording_id=recording.recording_id,
            recording_object_ref=recording,
            algorithm_ref=submission.algorithm_ref,
            config_ref=submission.config_ref,
            dependency_refs=dependencies,
            requested_output_schema=submission.requested_output_schema,
        )
        payload = recording_analysis_payload(request)
        job_id = recording_analysis_job_id(payload)
        self._jobs.enqueue(job_id, JobType.RECORDING_ANALYSIS, payload)
        return SubmittedRecordingAnalysis(job_id, payload, request)


def recording_analysis_job_id(payload: JobPayload) -> JobId:
    """Derive the stable job identity from the complete strict command."""

    digest = Digest.sha256(
        canonical_json_bytes(
            {
                "schema_id": payload.schema.schema_id,
                "schema_version": str(payload.schema.version),
                "value": thaw_value(payload.value),
            }
        )
    )
    return JobId(f"job_{digest.value}")
