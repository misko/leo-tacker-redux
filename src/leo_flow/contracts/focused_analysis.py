"""Contracts for capture-safe analysis of one synchronized focused pair."""

from __future__ import annotations

from dataclasses import dataclass

from leo_flow.contracts.core import (
    CaptureBatchId,
    Digest,
    JobId,
    RecordingId,
    canonical_digest,
)


@dataclass(frozen=True, slots=True)
class FocusedAnalysisPairScopeV0_1:
    """Exact terminal pair and six jobs authorized to overlap later captures."""

    capture_definition_digest: Digest
    batch_id: CaptureBatchId
    recording_ids: tuple[RecordingId, RecordingId]
    recording_identity_digests: tuple[Digest, Digest]
    feature_job_ids: tuple[JobId, JobId]
    waterfall_job_ids: tuple[JobId, JobId]
    starlink_suite_job_ids: tuple[JobId, JobId]

    def __post_init__(self) -> None:
        if len(set(self.recording_ids)) != 2:
            raise ValueError("focused analysis recording identities must be unique")
        if len(set(self.recording_identity_digests)) != 2:
            raise ValueError("focused analysis recording digests must be unique")
        all_jobs = (
            *self.feature_job_ids,
            *self.waterfall_job_ids,
            *self.starlink_suite_job_ids,
        )
        if len(set(all_jobs)) != 6:
            raise ValueError("focused analysis job identities must be unique")

    @property
    def identity_digest(self) -> Digest:
        return canonical_digest(self)
