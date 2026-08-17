"""Capture-closed staged analysis within the finite canary component."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from leo_flow.capture.supercycle_canary import (
    CANARY_ANALYSIS_STAGES,
    CANARY_COMPUTE_WORKERS,
    CANARY_PROJECTION_WORKERS,
    CANARY_RECORDINGS,
    CANARY_SLOTS,
    CanaryPhase,
    CanaryStageBenchmark,
    SupercycleCanaryCoordinator,
    SupercycleCanaryDefinition,
)
from leo_flow.contracts.capture_batch import CaptureBatchSnapshot
from leo_flow.contracts.core import UtcNs, canonical_digest
from leo_flow.contracts.deferred_analysis import (
    DeferredAnalysisLaneResultV1,
    DeferredAnalysisLaneState,
    DeferredAnalysisStage,
    DeferredAnalysisWindowV1,
)


class CanaryAnalysisError(RuntimeError):
    """The sealed canary analysis cannot safely advance."""


class CanaryWindowPreparerV1(Protocol):
    def prepare(
        self,
        definition: SupercycleCanaryDefinition,
        first_success_index: int,
        snapshots: tuple[CaptureBatchSnapshot, ...],
    ) -> DeferredAnalysisWindowV1: ...


class CanaryAnalysisLaneV1(Protocol):
    def drain(
        self,
        window: DeferredAnalysisWindowV1,
        stage: DeferredAnalysisStage,
        *,
        workers: int,
        deadline_utc_ns: UtcNs,
    ) -> DeferredAnalysisLaneResultV1: ...


@dataclass(frozen=True, slots=True)
class CanaryAnalysisRun:
    window: DeferredAnalysisWindowV1
    benchmarks: tuple[CanaryStageBenchmark, ...]


class SupercycleCanaryStagedAnalysis:
    """Run all six 0030-backed barriers once, after exact capture closure."""

    def __init__(
        self,
        definition: SupercycleCanaryDefinition,
        coordinator: SupercycleCanaryCoordinator,
        preparer: CanaryWindowPreparerV1,
        lane: CanaryAnalysisLaneV1,
        *,
        monotonic_ns: Callable[[], int],
        process_time_ns: Callable[[], int],
        peak_rss_bytes: Callable[[], int],
    ) -> None:
        self._definition = definition
        self._coordinator = coordinator
        self._preparer = preparer
        self._lane = lane
        self._monotonic_ns = monotonic_ns
        self._process_time_ns = process_time_ns
        self._peak_rss_bytes = peak_rss_bytes

    def run(self, *, deadline_utc_ns: UtcNs) -> CanaryAnalysisRun:
        state = self._coordinator.journal.load(self._definition)
        if (
            state.phase is not CanaryPhase.ANALYZING
            or state.captured_count != CANARY_SLOTS
            or len(state.records) != CANARY_SLOTS
        ):
            raise CanaryAnalysisError("canary capture closure is not exact")
        snapshots = tuple(record.snapshot for record in state.records)
        if any(snapshot is None for snapshot in snapshots):
            raise CanaryAnalysisError("canary capture evidence is incomplete")
        exact = tuple(snapshot for snapshot in snapshots if snapshot is not None)
        window = self._preparer.prepare(self._definition, 0, exact)
        self._validate_window(window, exact)
        benchmarks: list[CanaryStageBenchmark] = []
        for stage in DeferredAnalysisStage:
            workers = (
                CANARY_COMPUTE_WORKERS
                if stage.value.endswith("compute")
                else CANARY_PROJECTION_WORKERS
            )
            wall_start = self._monotonic_ns()
            cpu_start = self._process_time_ns()
            result = self._lane.drain(
                window, stage, workers=workers, deadline_utc_ns=deadline_utc_ns
            )
            cpu_end = self._process_time_ns()
            wall_end = self._monotonic_ns()
            if (
                result.stage is not stage
                or result.state is not DeferredAnalysisLaneState.COMPLETE
                or result.expected_count != CANARY_RECORDINGS
                or result.succeeded_count != CANARY_RECORDINGS
            ):
                self._coordinator.halt_analysis()
                raise CanaryAnalysisError(f"canary stage {stage.value} did not close")
            benchmarks.append(
                CanaryStageBenchmark(
                    stage.value,
                    workers,
                    wall_end - wall_start,
                    cpu_end - cpu_start,
                    self._peak_rss_bytes(),
                )
            )
        if tuple(item.stage for item in benchmarks) != CANARY_ANALYSIS_STAGES:
            raise CanaryAnalysisError("canary stage order differs")
        return CanaryAnalysisRun(window, tuple(benchmarks))

    def _validate_window(
        self,
        window: DeferredAnalysisWindowV1,
        snapshots: tuple[CaptureBatchSnapshot, ...],
    ) -> None:
        recording_ids = tuple(
            item.recording_id
            for snapshot in snapshots
            for item in sorted(
                snapshot.successful_recordings,
                key=lambda value: str(value.recording_id),
            )
        )
        recording_digests = tuple(
            canonical_digest(item.recording_object)
            for snapshot in snapshots
            for item in sorted(
                snapshot.successful_recordings,
                key=lambda value: str(value.recording_id),
            )
        )
        if (
            window.definition_digest != self._definition.digest
            or window.first_success_index != 0
            or window.batch_ids != tuple(snapshot.batch_id for snapshot in snapshots)
            or window.recording_ids != recording_ids
            or window.recording_identity_digests != recording_digests
        ):
            raise CanaryAnalysisError("canary staged window identities differ")
