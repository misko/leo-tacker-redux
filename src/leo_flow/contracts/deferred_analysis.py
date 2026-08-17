"""Additive contracts for campaign-scoped deferred-analysis windows."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from leo_flow.contracts.capture_batch import CaptureBatchSnapshot
from leo_flow.contracts.core import (
    CaptureBatchId,
    Digest,
    JobId,
    RecordingId,
    canonical_digest,
)

DEFERRED_ANALYSIS_WINDOW_BATCHES = 36
DEFERRED_ANALYSIS_RECORDINGS = 72
MAXIMUM_DEFERRED_COMPUTE_WORKERS = 8
MAXIMUM_DEFERRED_PROJECTION_WORKERS = 4


class DeferredAnalysisStage(str, Enum):
    FEATURE_COMPUTE = "feature_compute"
    FEATURE_PROJECTION = "feature_projection"
    WATERFALL_COMPUTE = "waterfall_compute"
    WATERFALL_PROJECTION = "waterfall_projection"
    STARLINK_SUITE_COMPUTE = "starlink_suite_compute"
    STARLINK_SUITE_PROJECTION = "starlink_suite_projection"


class DeferredAnalysisLaneState(str, Enum):
    COMPLETE = "complete"
    PENDING = "pending"
    PARKED = "parked"


class DeferredAnalysisCampaignPhase(str, Enum):
    ANALYZING = "analyzing"
    COMPLETE = "complete"


class DeferredAnalysisCampaignRecordPhase(str, Enum):
    CAPTURED = "captured"
    ANALYSIS_FAILED = "analysis_failed"


@dataclass(frozen=True, slots=True)
class OnlineAnalysisCampaignStateV1:
    """Read-only terminal-recording view while RF collection remains open."""

    definition_digest: Digest
    records: tuple[DeferredAnalysisCampaignRecordV1, ...]

    def __post_init__(self) -> None:
        if tuple(record.success_index for record in self.records) != tuple(
            range(len(self.records))
        ):
            raise ValueError("online analysis records are not contiguous")


@dataclass(frozen=True, slots=True)
class DeferredAnalysisCampaignDefinitionV1:
    digest: Digest
    qualification: bool
    analysis_after_each_capture: bool
    target_successes: int


@dataclass(frozen=True, slots=True)
class DeferredAnalysisCampaignRecordV1:
    success_index: int
    phase: DeferredAnalysisCampaignRecordPhase
    snapshot: CaptureBatchSnapshot | None


@dataclass(frozen=True, slots=True)
class DeferredAnalysisCampaignStateV1:
    definition_digest: Digest
    phase: DeferredAnalysisCampaignPhase
    analyzed_count: int
    records: tuple[DeferredAnalysisCampaignRecordV1, ...]


@dataclass(frozen=True, slots=True)
class DeferredAnalysisWindowV1:
    """Exact immutable identities for one balanced 36-batch window."""

    definition_digest: Digest
    first_success_index: int
    batch_ids: tuple[CaptureBatchId, ...]
    recording_ids: tuple[RecordingId, ...]
    recording_identity_digests: tuple[Digest, ...]
    feature_job_ids: tuple[JobId, ...]
    waterfall_job_ids: tuple[JobId, ...]
    starlink_suite_job_ids: tuple[JobId, ...]

    def __post_init__(self) -> None:
        if (
            self.first_success_index < 0
            or self.first_success_index % DEFERRED_ANALYSIS_WINDOW_BATCHES != 0
        ):
            raise ValueError("deferred window start is not a supercycle boundary")
        if len(self.batch_ids) != DEFERRED_ANALYSIS_WINDOW_BATCHES:
            raise ValueError("deferred window requires exactly 36 batches")
        if len(self.recording_ids) != DEFERRED_ANALYSIS_RECORDINGS:
            raise ValueError("deferred window requires exactly 72 recordings")
        if len(self.recording_identity_digests) != DEFERRED_ANALYSIS_RECORDINGS:
            raise ValueError("deferred window requires exactly 72 recording digests")
        for values, name in (
            (self.batch_ids, "batch"),
            (self.recording_ids, "recording"),
            (self.feature_job_ids, "feature job"),
            (self.waterfall_job_ids, "waterfall job"),
            (self.starlink_suite_job_ids, "Starlink-suite job"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"deferred window {name} identities are not unique")
        if any(
            len(values) != DEFERRED_ANALYSIS_RECORDINGS
            for values in (
                self.feature_job_ids,
                self.waterfall_job_ids,
                self.starlink_suite_job_ids,
            )
        ):
            raise ValueError("deferred window requires 72 jobs in every lane")

    @property
    def identity_digest(self) -> Digest:
        return canonical_digest(self)

    def job_ids(self, stage: DeferredAnalysisStage) -> tuple[JobId, ...]:
        if stage is DeferredAnalysisStage.FEATURE_COMPUTE:
            return self.feature_job_ids
        if stage is DeferredAnalysisStage.WATERFALL_COMPUTE:
            return self.waterfall_job_ids
        if stage is DeferredAnalysisStage.STARLINK_SUITE_COMPUTE:
            return self.starlink_suite_job_ids
        raise ValueError("projection stages do not carry job claim identities")


@dataclass(frozen=True, slots=True)
class DeferredAnalysisLaneResultV1:
    stage: DeferredAnalysisStage
    state: DeferredAnalysisLaneState
    expected_count: int
    succeeded_count: int
    retryable_count: int
    parked_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = (self.expected_count, self.succeeded_count, self.retryable_count)
        if any(value < 0 for value in values):
            raise ValueError("deferred lane counts must be non-negative")
        if self.succeeded_count + self.retryable_count + len(self.parked_ids) != (
            self.expected_count
        ):
            raise ValueError("deferred lane counts do not close")
        if len(self.parked_ids) != len(set(self.parked_ids)):
            raise ValueError("deferred lane parked identities are not unique")
        if self.state is DeferredAnalysisLaneState.COMPLETE and (
            self.succeeded_count != self.expected_count
            or self.retryable_count
            or self.parked_ids
        ):
            raise ValueError("complete deferred lane has unfinished work")
        if self.state is DeferredAnalysisLaneState.PENDING and (
            not self.retryable_count or self.parked_ids
        ):
            raise ValueError("pending deferred lane state differs from counts")
        if self.state is DeferredAnalysisLaneState.PARKED and not self.parked_ids:
            raise ValueError("parked deferred lane has no parked identity")
