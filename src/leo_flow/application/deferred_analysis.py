"""Bounded stage barriers for capture-first campaign analysis."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from leo_flow.contracts.capture_batch import CaptureBatchSnapshot
from leo_flow.contracts.core import Digest, UtcNs, canonical_digest
from leo_flow.contracts.deferred_analysis import (
    DEFERRED_ANALYSIS_RECORDINGS,
    DEFERRED_ANALYSIS_WINDOW_BATCHES,
    MAXIMUM_DEFERRED_COMPUTE_WORKERS,
    MAXIMUM_DEFERRED_PROJECTION_WORKERS,
    DeferredAnalysisCampaignDefinitionV1,
    DeferredAnalysisCampaignPhase,
    DeferredAnalysisCampaignRecordPhase,
    DeferredAnalysisCampaignRecordV1,
    DeferredAnalysisCampaignStateV1,
    DeferredAnalysisLaneResultV1,
    DeferredAnalysisLaneState,
    DeferredAnalysisStage,
    DeferredAnalysisWindowV1,
    OnlineAnalysisCampaignStateV1,
)


class DeferredAnalysisWindowError(RuntimeError):
    """The exact staged window cannot safely advance."""


class DeferredAnalysisWindowStatus(str, Enum):
    ADVANCED = "advanced"
    PENDING = "pending"
    PARKED = "parked"
    COMPLETE = "complete"


class OnlineAnalysisWindowStatus(str, Enum):
    ADVANCED = "advanced"
    PENDING = "pending"
    PARKED = "parked"
    CAUGHT_UP = "caught_up"


@dataclass(frozen=True, slots=True)
class DeferredAnalysisWorkerPolicyV1:
    compute_workers: int = MAXIMUM_DEFERRED_COMPUTE_WORKERS
    projection_workers: int = MAXIMUM_DEFERRED_PROJECTION_WORKERS

    def __post_init__(self) -> None:
        if isinstance(self.compute_workers, bool) or not (
            1 <= self.compute_workers <= MAXIMUM_DEFERRED_COMPUTE_WORKERS
        ):
            raise ValueError("deferred compute workers must be within 1..8")
        if isinstance(self.projection_workers, bool) or not (
            1 <= self.projection_workers <= MAXIMUM_DEFERRED_PROJECTION_WORKERS
        ):
            raise ValueError("deferred projection workers must be within 1..4")


class DeferredAnalysisWindowPreparerV1(Protocol):
    def prepare(
        self,
        definition: DeferredAnalysisCampaignDefinitionV1,
        first_success_index: int,
        snapshots: tuple[CaptureBatchSnapshot, ...],
    ) -> DeferredAnalysisWindowV1: ...


class DeferredAnalysisLaneV1(Protocol):
    def drain(
        self,
        window: DeferredAnalysisWindowV1,
        stage: DeferredAnalysisStage,
        *,
        workers: int,
        deadline_utc_ns: UtcNs,
    ) -> DeferredAnalysisLaneResultV1: ...


class OnlineDeferredAnalysisLaneV1(DeferredAnalysisLaneV1, Protocol):
    def inspect(
        self, window: DeferredAnalysisWindowV1, stage: DeferredAnalysisStage
    ) -> DeferredAnalysisLaneResultV1: ...


class DeferredAnalysisReceiptReconcilerV1(Protocol):
    def reconcile_one(self, *, deadline_utc_ns: UtcNs) -> bool: ...


@dataclass(frozen=True, slots=True)
class DeferredAnalysisWindowRunV1:
    status: DeferredAnalysisWindowStatus
    first_success_index: int | None
    reconciled_count: int = 0
    terminal_stage: DeferredAnalysisStage | None = None
    parked_ids: tuple[str, ...] = ()
    window_digest: Digest | None = None


@dataclass(frozen=True, slots=True)
class OnlineAnalysisWindowRunV1:
    status: OnlineAnalysisWindowStatus
    first_success_index: int | None
    terminal_stage: DeferredAnalysisStage | None = None
    parked_ids: tuple[str, ...] = ()
    window_digest: Digest | None = None


_STAGES = (
    DeferredAnalysisStage.FEATURE_COMPUTE,
    DeferredAnalysisStage.FEATURE_PROJECTION,
    DeferredAnalysisStage.WATERFALL_COMPUTE,
    DeferredAnalysisStage.WATERFALL_PROJECTION,
    DeferredAnalysisStage.STARLINK_SUITE_COMPUTE,
    DeferredAnalysisStage.STARLINK_SUITE_PROJECTION,
)


class ExactDeferredAnalysisWindowCoordinatorV1:
    """Advance one exact supercycle without weakening campaign receipts.

    Durable PostgreSQL jobs/projections are the restart journal for stage work.
    The existing SQLite campaign journal remains the authority for serial exact
    receipt reconciliation.
    """

    def __init__(
        self,
        definition: DeferredAnalysisCampaignDefinitionV1,
        preparer: DeferredAnalysisWindowPreparerV1,
        lane: DeferredAnalysisLaneV1,
        reconciler: DeferredAnalysisReceiptReconcilerV1,
        policy: DeferredAnalysisWorkerPolicyV1,
    ) -> None:
        if definition.qualification or definition.analysis_after_each_capture:
            raise ValueError("staged analysis requires a deferred main campaign")
        if (
            definition.target_successes % DEFERRED_ANALYSIS_WINDOW_BATCHES != 0
            or definition.target_successes != 936
        ):
            raise ValueError("staged analysis requires the reviewed 936-batch target")
        self._definition = definition
        self._preparer = preparer
        self._lane = lane
        self._reconciler = reconciler
        self._policy = policy

    def advance_window(
        self, state: DeferredAnalysisCampaignStateV1, *, deadline_utc_ns: UtcNs
    ) -> DeferredAnalysisWindowRunV1:
        if state.definition_digest != self._definition.digest:
            raise DeferredAnalysisWindowError("campaign definition digest differs")
        if state.phase is DeferredAnalysisCampaignPhase.COMPLETE:
            if state.analyzed_count != self._definition.target_successes:
                raise DeferredAnalysisWindowError("complete campaign count differs")
            return DeferredAnalysisWindowRunV1(
                DeferredAnalysisWindowStatus.COMPLETE, None
            )
        if state.phase is not DeferredAnalysisCampaignPhase.ANALYZING:
            raise DeferredAnalysisWindowError("campaign analysis phase is not open")
        records = self._pending_window(state)
        first = records[0].success_index
        snapshots = tuple(self._snapshot(record) for record in records)
        window = self._preparer.prepare(self._definition, first, snapshots)
        self._validate_window(window, records)
        for stage in _STAGES:
            workers = (
                self._policy.compute_workers
                if stage
                in {
                    DeferredAnalysisStage.FEATURE_COMPUTE,
                    DeferredAnalysisStage.WATERFALL_COMPUTE,
                    DeferredAnalysisStage.STARLINK_SUITE_COMPUTE,
                }
                else self._policy.projection_workers
            )
            result = self._lane.drain(
                window, stage, workers=workers, deadline_utc_ns=deadline_utc_ns
            )
            if result.stage is not stage:
                raise DeferredAnalysisWindowError("deferred lane stage differs")
            if result.expected_count != DEFERRED_ANALYSIS_RECORDINGS:
                raise DeferredAnalysisWindowError(
                    "deferred lane expected count differs"
                )
            if result.state is DeferredAnalysisLaneState.PARKED:
                return DeferredAnalysisWindowRunV1(
                    DeferredAnalysisWindowStatus.PARKED,
                    first,
                    terminal_stage=stage,
                    parked_ids=result.parked_ids,
                    window_digest=window.identity_digest,
                )
            if result.state is DeferredAnalysisLaneState.PENDING:
                return DeferredAnalysisWindowRunV1(
                    DeferredAnalysisWindowStatus.PENDING,
                    first,
                    terminal_stage=stage,
                    window_digest=window.identity_digest,
                )
            if result.state is not DeferredAnalysisLaneState.COMPLETE:
                raise DeferredAnalysisWindowError("deferred lane state differs")
        reconciled = 0
        for _record in records:
            if not self._reconciler.reconcile_one(deadline_utc_ns=deadline_utc_ns):
                return DeferredAnalysisWindowRunV1(
                    DeferredAnalysisWindowStatus.PENDING,
                    first,
                    reconciled_count=reconciled,
                    window_digest=window.identity_digest,
                )
            reconciled += 1
        return DeferredAnalysisWindowRunV1(
            DeferredAnalysisWindowStatus.ADVANCED,
            first,
            reconciled_count=reconciled,
            window_digest=window.identity_digest,
        )

    def _pending_window(
        self, state: DeferredAnalysisCampaignStateV1
    ) -> tuple[DeferredAnalysisCampaignRecordV1, ...]:
        pending = tuple(
            record
            for record in state.records
            if record.phase
            in {
                DeferredAnalysisCampaignRecordPhase.CAPTURED,
                DeferredAnalysisCampaignRecordPhase.ANALYSIS_FAILED,
            }
        )
        if not pending:
            raise DeferredAnalysisWindowError("analysis phase has no pending window")
        first = pending[0].success_index
        if first % DEFERRED_ANALYSIS_WINDOW_BATCHES:
            raise DeferredAnalysisWindowError(
                "pending window is not supercycle aligned"
            )
        if len(pending) < DEFERRED_ANALYSIS_WINDOW_BATCHES:
            raise DeferredAnalysisWindowError("deferred final window is incomplete")
        records = pending[:DEFERRED_ANALYSIS_WINDOW_BATCHES]
        if tuple(record.success_index for record in records) != tuple(
            range(first, first + DEFERRED_ANALYSIS_WINDOW_BATCHES)
        ):
            raise DeferredAnalysisWindowError(
                "pending window identities are not contiguous"
            )
        return records

    @staticmethod
    def _snapshot(record: DeferredAnalysisCampaignRecordV1) -> CaptureBatchSnapshot:
        if record.snapshot is None:
            raise DeferredAnalysisWindowError("pending record has no capture snapshot")
        if len(record.snapshot.successful_recordings) != 2:
            raise DeferredAnalysisWindowError(
                "pending batch does not have two recordings"
            )
        return record.snapshot

    def _validate_window(
        self,
        window: DeferredAnalysisWindowV1,
        records: tuple[DeferredAnalysisCampaignRecordV1, ...],
    ) -> None:
        snapshots = tuple(self._snapshot(record) for record in records)
        expected_recordings = tuple(
            recording.recording_id
            for snapshot in snapshots
            for recording in sorted(
                snapshot.successful_recordings, key=lambda item: str(item.recording_id)
            )
        )
        expected_recording_digests = tuple(
            canonical_digest(recording.recording_object)
            for snapshot in snapshots
            for recording in sorted(
                snapshot.successful_recordings, key=lambda item: str(item.recording_id)
            )
        )
        if (
            window.definition_digest != self._definition.digest
            or window.first_success_index != records[0].success_index
            or window.batch_ids != tuple(snapshot.batch_id for snapshot in snapshots)
            or window.recording_ids != expected_recordings
            or window.recording_identity_digests != expected_recording_digests
        ):
            raise DeferredAnalysisWindowError("prepared window identities differ")


class OnlineDeferredAnalysisWindowCoordinatorV1:
    """Process complete windows while collection stays read-only and open."""

    def __init__(
        self,
        definition: DeferredAnalysisCampaignDefinitionV1,
        preparer: DeferredAnalysisWindowPreparerV1,
        lane: OnlineDeferredAnalysisLaneV1,
        policy: DeferredAnalysisWorkerPolicyV1,
    ) -> None:
        if definition.qualification or definition.analysis_after_each_capture:
            raise ValueError("online analysis requires a deferred main campaign")
        if definition.target_successes != 936:
            raise ValueError("online analysis requires the reviewed 936-batch target")
        self._definition = definition
        self._preparer = preparer
        self._lane = lane
        self._policy = policy

    def advance_available(
        self, state: OnlineAnalysisCampaignStateV1, *, deadline_utc_ns: UtcNs
    ) -> OnlineAnalysisWindowRunV1:
        if state.definition_digest != self._definition.digest:
            raise DeferredAnalysisWindowError("campaign definition digest differs")
        complete_windows = len(state.records) // DEFERRED_ANALYSIS_WINDOW_BATCHES
        for number in range(complete_windows):
            first = number * DEFERRED_ANALYSIS_WINDOW_BATCHES
            records = state.records[first : first + DEFERRED_ANALYSIS_WINDOW_BATCHES]
            snapshots = tuple(_online_snapshot(record) for record in records)
            window = self._preparer.prepare(self._definition, first, snapshots)
            _validate_online_window(self._definition, window, records)
            changed = False
            for stage in _STAGES:
                current = self._lane.inspect(window, stage)
                _validate_lane_result(stage, current)
                if current.state is DeferredAnalysisLaneState.PARKED:
                    return OnlineAnalysisWindowRunV1(
                        OnlineAnalysisWindowStatus.PARKED,
                        first,
                        stage,
                        current.parked_ids,
                        window.identity_digest,
                    )
                if current.state is DeferredAnalysisLaneState.COMPLETE:
                    continue
                workers = (
                    self._policy.compute_workers
                    if stage
                    in {
                        DeferredAnalysisStage.FEATURE_COMPUTE,
                        DeferredAnalysisStage.WATERFALL_COMPUTE,
                        DeferredAnalysisStage.STARLINK_SUITE_COMPUTE,
                    }
                    else self._policy.projection_workers
                )
                result = self._lane.drain(
                    window, stage, workers=workers, deadline_utc_ns=deadline_utc_ns
                )
                _validate_lane_result(stage, result)
                if result.state is DeferredAnalysisLaneState.PARKED:
                    return OnlineAnalysisWindowRunV1(
                        OnlineAnalysisWindowStatus.PARKED,
                        first,
                        stage,
                        result.parked_ids,
                        window.identity_digest,
                    )
                if result.state is DeferredAnalysisLaneState.PENDING:
                    return OnlineAnalysisWindowRunV1(
                        OnlineAnalysisWindowStatus.PENDING,
                        first,
                        stage,
                        window_digest=window.identity_digest,
                    )
                changed = True
            if changed:
                return OnlineAnalysisWindowRunV1(
                    OnlineAnalysisWindowStatus.ADVANCED,
                    first,
                    window_digest=window.identity_digest,
                )
        return OnlineAnalysisWindowRunV1(OnlineAnalysisWindowStatus.CAUGHT_UP, None)


def _online_snapshot(
    record: DeferredAnalysisCampaignRecordV1,
) -> CaptureBatchSnapshot:
    if (
        record.phase
        not in {
            DeferredAnalysisCampaignRecordPhase.CAPTURED,
            DeferredAnalysisCampaignRecordPhase.ANALYSIS_FAILED,
        }
        or record.snapshot is None
    ):
        raise DeferredAnalysisWindowError("online record is not terminal captured")
    if not record.snapshot.terminal or len(record.snapshot.successful_recordings) != 2:
        raise DeferredAnalysisWindowError(
            "online batch does not have two terminal recordings"
        )
    return record.snapshot


def _validate_online_window(
    definition: DeferredAnalysisCampaignDefinitionV1,
    window: DeferredAnalysisWindowV1,
    records: tuple[DeferredAnalysisCampaignRecordV1, ...],
) -> None:
    snapshots = tuple(_online_snapshot(record) for record in records)
    expected_recordings = tuple(
        recording.recording_id
        for snapshot in snapshots
        for recording in sorted(
            snapshot.successful_recordings, key=lambda item: str(item.recording_id)
        )
    )
    expected_digests = tuple(
        canonical_digest(recording.recording_object)
        for snapshot in snapshots
        for recording in sorted(
            snapshot.successful_recordings, key=lambda item: str(item.recording_id)
        )
    )
    if (
        window.definition_digest != definition.digest
        or window.first_success_index != records[0].success_index
        or window.batch_ids != tuple(snapshot.batch_id for snapshot in snapshots)
        or window.recording_ids != expected_recordings
        or window.recording_identity_digests != expected_digests
    ):
        raise DeferredAnalysisWindowError("prepared window identities differ")


def _validate_lane_result(
    stage: DeferredAnalysisStage, result: DeferredAnalysisLaneResultV1
) -> None:
    if result.stage is not stage:
        raise DeferredAnalysisWindowError("deferred lane stage differs")
    if result.expected_count != DEFERRED_ANALYSIS_RECORDINGS:
        raise DeferredAnalysisWindowError("deferred lane expected count differs")
