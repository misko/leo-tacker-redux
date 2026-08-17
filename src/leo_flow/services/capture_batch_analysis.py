"""Submit per-recording analysis only from one closed public batch snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.capture_batch import (
    CaptureBatchSnapshot,
    PairedAnalysisEligibility,
)
from leo_flow.contracts.core import ArtifactRef, RecordingId, SchemaRef
from leo_flow.contracts.features import FeatureSetBundle
from leo_flow.contracts.storage import PublishedRecordingRef

from .recording_submission import (
    RecordingAnalysisJobEnqueuer,
    RecordingAnalysisSubmission,
    RecordingAnalysisSubmissionService,
    SubmittedRecordingAnalysis,
)


class ClosedBatchAnalysisSubmissionError(RuntimeError):
    """The supplied terminal batch does not match authoritative publications."""


class PublishedRecordingCatalog(Protocol):
    def get(self, recording_id: RecordingId) -> PublishedRecordingRef | None: ...


@dataclass(frozen=True)
class ClosedBatchAnalysisSelection:
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    dependency_refs: tuple[ArtifactRef, ...]
    requested_output_schema: SchemaRef

    def __post_init__(self) -> None:
        if self.requested_output_schema != SchemaRef(FeatureSetBundle.SCHEMA_ID):
            raise ValueError("closed-batch output schema is unsupported")
        dependency_ids = tuple(item.artifact_id for item in self.dependency_refs)
        if len(dependency_ids) != len(set(dependency_ids)):
            raise ValueError("closed-batch dependency IDs must be unique")


@dataclass(frozen=True)
class SubmittedClosedBatchAnalysis:
    snapshot: CaptureBatchSnapshot
    paired_analysis_eligibility: PairedAnalysisEligibility
    recording_jobs: tuple[SubmittedRecordingAnalysis, ...]


class ClosedBatchAnalysisSubmissionService:
    """Verify a terminal snapshot and enqueue canonical per-recording work.

    Paired eligibility is returned as an observed public batch fact.  This
    service never creates or implies a paired-science request.
    """

    def __init__(
        self,
        recordings: PublishedRecordingCatalog,
        jobs: RecordingAnalysisJobEnqueuer,
    ) -> None:
        self._recordings = recordings
        self._submission = RecordingAnalysisSubmissionService(jobs)

    def submit(
        self,
        snapshot: CaptureBatchSnapshot,
        selection: ClosedBatchAnalysisSelection,
    ) -> SubmittedClosedBatchAnalysis:
        if not snapshot.terminal:
            raise ClosedBatchAnalysisSubmissionError(
                "capture batch must be terminal before analysis submission"
            )

        authoritative: list[PublishedRecordingRef] = []
        for claimed in snapshot.successful_recordings:
            recording = self._recordings.get(claimed.recording_id)
            if recording is None or recording != claimed:
                raise ClosedBatchAnalysisSubmissionError(
                    "batch recording differs from the public recording catalog"
                )
            authoritative.append(recording)

        submitted = tuple(
            self._submission.submit(
                RecordingAnalysisSubmission(
                    recording=recording,
                    algorithm_ref=selection.algorithm_ref,
                    config_ref=selection.config_ref,
                    dependency_refs=selection.dependency_refs,
                    requested_output_schema=selection.requested_output_schema,
                )
            )
            for recording in authoritative
        )
        return SubmittedClosedBatchAnalysis(
            snapshot,
            snapshot.paired_analysis_eligibility,
            submitted,
        )
