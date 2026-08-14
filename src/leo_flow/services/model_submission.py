"""Exact, restart-safe submission of cross-recording model analysis jobs.

Submission coordinates analysis authority resolution with the durable job
repository, so it belongs to the service layer rather than the dependency-light
application projection package.
"""

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
from leo_flow.analysis.model.tracking_input_persistence import (
    DurableTrackingInputView,
)
from leo_flow.contracts._validation import thaw_value
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    JobId,
    SchemaRef,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.ports import FeatureSetReader
from leo_flow.contracts.tracking_input import (
    TrackingInputSnapshotIdentity,
    TrackingInputSnapshotRef,
)
from leo_flow.contracts.tracking_model import TrackingModelAnalysisRequest
from leo_flow.jobs.contracts import JobPayload, JobType
from leo_flow.services.model_analysis import (
    model_analysis_payload,
    tracking_model_analysis_payload,
)


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


class TrackingInputAuthority(Protocol):
    """Resolve scientific identity without accepting an operational locator."""

    def get_by_identity(
        self, identity: TrackingInputSnapshotIdentity
    ) -> DurableTrackingInputView: ...


class TrackingModelSubmissionError(ValueError):
    """The authority returned a different identity than the exact command."""


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


@dataclass(frozen=True)
class TrackingModelAnalysisSubmission:
    """One locator-free tracking model command."""

    tracking_input_identity: TrackingInputSnapshotIdentity
    model_config_ref: ArtifactRef
    algorithm_ref: ArtifactRef


@dataclass(frozen=True)
class SubmittedTrackingModelAnalysis:
    """Durable command plus the current locator resolved for diagnostics."""

    job_id: JobId
    payload: JobPayload
    request: TrackingModelAnalysisRequest
    resolved_tracking_input_ref: TrackingInputSnapshotRef


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


class TrackingModelAnalysisSubmissionService:
    """Resolve one exact tracking join, then enqueue its locator-free identity."""

    def __init__(
        self,
        *,
        tracking_inputs: TrackingInputAuthority,
        jobs: ModelAnalysisJobEnqueuer,
    ) -> None:
        self._tracking_inputs = tracking_inputs
        self._jobs = jobs

    def submit(
        self, submission: TrackingModelAnalysisSubmission
    ) -> SubmittedTrackingModelAnalysis:
        resolved = self._tracking_inputs.get_by_identity(
            submission.tracking_input_identity
        )
        snapshot = resolved.snapshot
        identity = submission.tracking_input_identity
        if (
            not resolved.ref.matches_identity(identity)
            or snapshot.snapshot_id != identity.snapshot_id
            or snapshot.snapshot_digest != identity.snapshot_digest
            or snapshot.membership_digest != identity.membership_digest
        ):
            raise TrackingModelSubmissionError(
                "tracking input authority substituted an identity"
            )
        request = TrackingModelAnalysisRequest(
            schema=SchemaRef(TrackingModelAnalysisRequest.SCHEMA_ID),
            tracking_input_identity=submission.tracking_input_identity,
            model_config_ref=submission.model_config_ref,
            algorithm_ref=submission.algorithm_ref,
        )
        payload = tracking_model_analysis_payload(request)
        job_id = model_analysis_job_id(payload)
        self._jobs.enqueue(job_id, JobType.MODEL_ANALYSIS, payload)
        return SubmittedTrackingModelAnalysis(
            job_id,
            payload,
            request,
            resolved.ref,
        )


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
