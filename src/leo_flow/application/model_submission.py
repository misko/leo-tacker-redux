"""Exact, restart-safe submission of cross-recording model analysis jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from leo_flow.analysis.dataset import DatasetSnapshotReader, DatasetSnapshotRef
from leo_flow.analysis.model import (
    AssembledModelInputs,
    EphemerisLinkRequirement,
    assemble_model_inputs,
)
from leo_flow.analysis.model.inputs import (
    RecordingCatalogReader,
    RecordingEphemerisLinkReader,
    RecordingHardwareLinkReader,
)
from leo_flow.contracts._validation import thaw_value
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    JobId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.ports import FeatureSetReader
from leo_flow.jobs.contracts import JobPayload, JobType
from leo_flow.services.model_analysis import model_analysis_payload


class ModelAnalysisJobEnqueuer(Protocol):
    """The only queue capability required by model submission."""

    def enqueue(
        self,
        job_id: JobId,
        job_type: JobType,
        payload: JobPayload,
        *,
        available_at_utc_ns: UtcNs | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class ModelAnalysisSubmission:
    """One fully pinned request to close and enqueue model inputs."""

    dataset_ref: DatasetSnapshotRef
    ephemeris_requirement: EphemerisLinkRequirement
    model_config_ref: ArtifactRef
    algorithm_ref: ArtifactRef


@dataclass(frozen=True)
class SubmittedModelAnalysis:
    """The exact durable command created by a successful submission."""

    job_id: JobId
    payload: JobPayload
    assembled_inputs: AssembledModelInputs


class ModelAnalysisSubmissionService:
    """Resolve immutable authorities, then enqueue the existing typed job."""

    def __init__(
        self,
        *,
        datasets: DatasetSnapshotReader,
        features: FeatureSetReader,
        recordings: RecordingCatalogReader,
        hardware_links: RecordingHardwareLinkReader,
        ephemeris_links: RecordingEphemerisLinkReader,
        jobs: ModelAnalysisJobEnqueuer,
    ) -> None:
        self._datasets = datasets
        self._features = features
        self._recordings = recordings
        self._hardware_links = hardware_links
        self._ephemeris_links = ephemeris_links
        self._jobs = jobs

    def submit(self, submission: ModelAnalysisSubmission) -> SubmittedModelAnalysis:
        """Fail closed before crossing the durable enqueue boundary."""

        dataset = self._datasets.get(submission.dataset_ref)
        assembled = assemble_model_inputs(
            dataset=dataset,
            expected_dataset_ref=submission.dataset_ref,
            features=self._features,
            recordings=self._recordings,
            hardware_links=self._hardware_links,
            ephemeris_links=self._ephemeris_links,
            ephemeris_requirement=submission.ephemeris_requirement,
            model_config_ref=submission.model_config_ref,
            algorithm_ref=submission.algorithm_ref,
        )
        payload = model_analysis_payload(assembled.request, submission.dataset_ref)
        job_id = model_analysis_job_id(payload)
        self._jobs.enqueue(job_id, JobType.MODEL_ANALYSIS, payload)
        return SubmittedModelAnalysis(job_id, payload, assembled)


def model_analysis_job_id(payload: JobPayload) -> JobId:
    """Derive a stable content identity from the strict durable command."""

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
