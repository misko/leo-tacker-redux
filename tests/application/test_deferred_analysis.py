from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.application.deferred_analysis import (
    DeferredAnalysisWindowError,
    DeferredAnalysisWindowStatus,
    DeferredAnalysisWorkerPolicyV1,
    ExactDeferredAnalysisWindowCoordinatorV1,
    OnlineAnalysisWindowStatus,
    OnlineDeferredAnalysisWindowCoordinatorV1,
)
from leo_flow.capture.continuous import (
    ContinuousCollectionPhase,
    DeferredCampaignCoordinator,
    InMemoryContinuousCollectionJournal,
)
from leo_flow.contracts.core import JobId, UtcNs, canonical_digest
from leo_flow.contracts.deferred_analysis import (
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
from tests.capture.test_campaign import (
    START,
    _Analysis,
    _Capacity,
    _Capture,
    _main_definition,
)


def _state():
    definition, receipt = _main_definition()
    definition = replace(definition, analysis_after_each_capture=False)
    coordinator = DeferredCampaignCoordinator(
        definition,
        InMemoryContinuousCollectionJournal(),
        _Capture(),
        _Analysis(),
        _Capacity(),
        0,
        receipt,
    )
    state = None
    for index in range(36):
        requested = START + index * 400_000_000_000 // 13
        state = coordinator.capture_next(UtcNs(requested)).state
    assert state is not None
    state = replace(state, phase=ContinuousCollectionPhase.ANALYZING)
    return (
        DeferredAnalysisCampaignStateV1(
            state.definition_digest,
            DeferredAnalysisCampaignPhase.ANALYZING,
            state.analyzed_count,
            tuple(
                DeferredAnalysisCampaignRecordV1(
                    record.unit.success_index,
                    DeferredAnalysisCampaignRecordPhase.CAPTURED,
                    record.snapshot,
                )
                for record in state.records
            ),
        ),
        DeferredAnalysisCampaignDefinitionV1(
            definition.digest,
            definition.qualification,
            definition.analysis_after_each_capture,
            936,
        ),
    )


class _Preparer:
    def prepare(self, definition, first_success_index, snapshots):
        recordings = tuple(
            recording.recording_id
            for snapshot in snapshots
            for recording in sorted(
                snapshot.successful_recordings, key=lambda item: str(item.recording_id)
            )
        )
        return DeferredAnalysisWindowV1(
            definition.digest,
            first_success_index,
            tuple(snapshot.batch_id for snapshot in snapshots),
            recordings,
            tuple(
                canonical_digest(recording.recording_object)
                for snapshot in snapshots
                for recording in sorted(
                    snapshot.successful_recordings,
                    key=lambda item: str(item.recording_id),
                )
            ),
            tuple(JobId(f"job_feature_{index}") for index in range(72)),
            tuple(JobId(f"job_waterfall_{index}") for index in range(72)),
            tuple(JobId(f"job_suite_{index}") for index in range(72)),
        )


class _Lane:
    def __init__(self, stop=None):
        self.stop = stop
        self.calls = []

    def drain(self, window, stage, *, workers, deadline_utc_ns):
        del window, deadline_utc_ns
        self.calls.append((stage, workers))
        if self.stop == (stage, DeferredAnalysisLaneState.PENDING):
            return DeferredAnalysisLaneResultV1(stage, self.stop[1], 72, 71, 1)
        if self.stop == (stage, DeferredAnalysisLaneState.PARKED):
            return DeferredAnalysisLaneResultV1(
                stage, self.stop[1], 72, 71, 0, ("job_parked",)
            )
        return DeferredAnalysisLaneResultV1(
            stage, DeferredAnalysisLaneState.COMPLETE, 72, 72, 0
        )


class _Reconciler:
    def __init__(self, outcomes=None):
        self.outcomes = iter(outcomes or [True] * 36)
        self.calls = 0

    def reconcile_one(self, *, deadline_utc_ns):
        del deadline_utc_ns
        self.calls += 1
        return next(self.outcomes)


def test_exact_window_runs_stage_barriers_then_serial_reconciliation():
    state, definition = _state()
    lane, reconciler = _Lane(), _Reconciler()
    coordinator = ExactDeferredAnalysisWindowCoordinatorV1(
        definition, _Preparer(), lane, reconciler, DeferredAnalysisWorkerPolicyV1()
    )

    result = coordinator.advance_window(state, deadline_utc_ns=UtcNs(10**18))

    assert result.status is DeferredAnalysisWindowStatus.ADVANCED
    assert result.reconciled_count == 36
    assert reconciler.calls == 36
    assert [workers for _, workers in lane.calls] == [8, 4, 8, 4, 8, 4]


@pytest.mark.parametrize(
    "terminal", [DeferredAnalysisLaneState.PENDING, DeferredAnalysisLaneState.PARKED]
)
def test_window_stops_at_pending_or_parked_lane(terminal):
    state, definition = _state()
    stop = (DeferredAnalysisStage.WATERFALL_COMPUTE, terminal)
    lane, reconciler = _Lane(stop), _Reconciler()
    coordinator = ExactDeferredAnalysisWindowCoordinatorV1(
        definition, _Preparer(), lane, reconciler, DeferredAnalysisWorkerPolicyV1(6, 3)
    )

    result = coordinator.advance_window(state, deadline_utc_ns=UtcNs(10**18))

    assert result.status.value == terminal.value
    assert reconciler.calls == 0
    assert lane.calls == [
        (DeferredAnalysisStage.FEATURE_COMPUTE, 6),
        (DeferredAnalysisStage.FEATURE_PROJECTION, 3),
        (DeferredAnalysisStage.WATERFALL_COMPUTE, 6),
    ]


@pytest.mark.parametrize("expected_count", [0, 71, 73])
def test_window_rejects_complete_lane_with_wrong_expected_count(
    expected_count,
):
    state, definition = _state()

    class WrongCountLane:
        def drain(self, window, stage, *, workers, deadline_utc_ns):
            del window, workers, deadline_utc_ns
            return DeferredAnalysisLaneResultV1(
                stage,
                DeferredAnalysisLaneState.COMPLETE,
                expected_count,
                expected_count,
                0,
            )

    coordinator = ExactDeferredAnalysisWindowCoordinatorV1(
        definition,
        _Preparer(),
        WrongCountLane(),
        _Reconciler(),
        DeferredAnalysisWorkerPolicyV1(),
    )

    with pytest.raises(DeferredAnalysisWindowError, match="expected count differs"):
        coordinator.advance_window(state, deadline_utc_ns=UtcNs(10**18))


def test_window_rejects_non_supercycle_pending_boundary():
    state, definition = _state()
    broken = replace(state, records=state.records[1:])
    coordinator = ExactDeferredAnalysisWindowCoordinatorV1(
        definition,
        _Preparer(),
        _Lane(),
        _Reconciler(),
        DeferredAnalysisWorkerPolicyV1(),
    )
    with pytest.raises(DeferredAnalysisWindowError, match="supercycle aligned"):
        coordinator.advance_window(broken, deadline_utc_ns=UtcNs(10**18))


@pytest.mark.parametrize("compute,projection", [(0, 1), (9, 1), (1, 0), (1, 5)])
def test_worker_policy_is_strictly_bounded(compute, projection):
    with pytest.raises(ValueError):
        DeferredAnalysisWorkerPolicyV1(compute, projection)


class _OnlineLane:
    def __init__(self):
        self.complete = set()
        self.drains = []

    def inspect(self, window, stage):
        del window
        if stage in self.complete:
            return DeferredAnalysisLaneResultV1(
                stage, DeferredAnalysisLaneState.COMPLETE, 72, 72, 0
            )
        return DeferredAnalysisLaneResultV1(
            stage, DeferredAnalysisLaneState.PENDING, 72, 0, 72
        )

    def drain(self, window, stage, *, workers, deadline_utc_ns):
        del window, deadline_utc_ns
        self.drains.append((stage, workers))
        self.complete.add(stage)
        return DeferredAnalysisLaneResultV1(
            stage, DeferredAnalysisLaneState.COMPLETE, 72, 72, 0
        )


def test_online_window_processes_terminal_recordings_without_reconciliation():
    state, definition = _state()
    lane = _OnlineLane()
    coordinator = OnlineDeferredAnalysisWindowCoordinatorV1(
        definition, _Preparer(), lane, DeferredAnalysisWorkerPolicyV1(6, 3)
    )
    online_state = OnlineAnalysisCampaignStateV1(state.definition_digest, state.records)

    result = coordinator.advance_available(online_state, deadline_utc_ns=UtcNs(10**18))

    assert result.status is OnlineAnalysisWindowStatus.ADVANCED
    assert result.first_success_index == 0
    assert [workers for _, workers in lane.drains] == [6, 3, 6, 3, 6, 3]
    assert state.analyzed_count == 0
    assert (
        coordinator.advance_available(
            online_state, deadline_utc_ns=UtcNs(10**18)
        ).status
        is OnlineAnalysisWindowStatus.CAUGHT_UP
    )


def test_online_window_does_not_prepare_an_incomplete_capture_window():
    state, definition = _state()

    class NeverPrepare:
        def prepare(self, *_args):
            raise AssertionError("in-flight window reached analysis preparation")

    coordinator = OnlineDeferredAnalysisWindowCoordinatorV1(
        definition, NeverPrepare(), _OnlineLane(), DeferredAnalysisWorkerPolicyV1()
    )
    partial = OnlineAnalysisCampaignStateV1(state.definition_digest, state.records[:35])

    assert (
        coordinator.advance_available(partial, deadline_utc_ns=UtcNs(10**18)).status
        is OnlineAnalysisWindowStatus.CAUGHT_UP
    )


def test_online_window_rejects_foreign_prepared_recording_before_claim():
    state, definition = _state()
    good = _Preparer().prepare(definition, 0, tuple(r.snapshot for r in state.records))

    class ForeignPreparer:
        def prepare(self, *_args):
            return replace(
                good,
                recording_ids=tuple(reversed(good.recording_ids)),
            )

    lane = _OnlineLane()
    coordinator = OnlineDeferredAnalysisWindowCoordinatorV1(
        definition,
        ForeignPreparer(),
        lane,
        DeferredAnalysisWorkerPolicyV1(),
    )
    with pytest.raises(DeferredAnalysisWindowError, match="identities differ"):
        coordinator.advance_available(
            OnlineAnalysisCampaignStateV1(state.definition_digest, state.records),
            deadline_utc_ns=UtcNs(10**18),
        )
    assert lane.drains == []
