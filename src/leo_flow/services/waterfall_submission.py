"""Exact idempotent submission of one published recording for waterfall analysis."""

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
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.contracts.waterfall import (
    WaterfallAnalysisRequestV0_1,
    WaterfallBundleV0_1,
)
from leo_flow.jobs.contracts import JobPayload, JobType

from .waterfall_analysis import waterfall_analysis_payload


class WaterfallJobEnqueuer(Protocol):
    def enqueue(
        self,
        job_id: JobId,
        job_type: JobType,
        payload: JobPayload,
        *,
        available_at_utc_ns: UtcNs | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class WaterfallAnalysisSubmissionV0_1:
    recording: PublishedRecordingRef
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    dependency_refs: tuple[ArtifactRef, ...]

    def __post_init__(self) -> None:
        ids = tuple(item.artifact_id for item in self.dependency_refs)
        if len(ids) != len(set(ids)):
            raise ValueError("waterfall dependency IDs must be unique")


@dataclass(frozen=True)
class SubmittedWaterfallAnalysisV0_1:
    job_id: JobId
    payload: JobPayload
    request: WaterfallAnalysisRequestV0_1


class WaterfallAnalysisSubmissionServiceV0_1:
    def __init__(self, jobs: WaterfallJobEnqueuer) -> None:
        self._jobs = jobs

    def submit(
        self, submission: WaterfallAnalysisSubmissionV0_1
    ) -> SubmittedWaterfallAnalysisV0_1:
        recording = submission.recording.recording_object
        dependencies = tuple(
            sorted(
                submission.dependency_refs,
                key=lambda item: (item.artifact_id, str(item.digest)),
            )
        )
        request = WaterfallAnalysisRequestV0_1(
            SchemaRef(WaterfallAnalysisRequestV0_1.SCHEMA_ID),
            recording.recording_id,
            recording,
            submission.algorithm_ref,
            submission.config_ref,
            dependencies,
            SchemaRef(WaterfallBundleV0_1.SCHEMA_ID),
        )
        payload = waterfall_analysis_payload(request)
        job_id = waterfall_analysis_job_id(payload)
        self._jobs.enqueue(job_id, JobType.WATERFALL_ANALYSIS, payload)
        return SubmittedWaterfallAnalysisV0_1(job_id, payload, request)


def waterfall_analysis_job_id(payload: JobPayload) -> JobId:
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
