from __future__ import annotations

import multiprocessing
import os
import time
from dataclasses import dataclass

import pytest

from leo_flow.contracts.core import CaptureBatchId, Digest, JobId, RecordingId, UtcNs
from leo_flow.contracts.deferred_analysis import (
    DeferredAnalysisLaneState,
    DeferredAnalysisStage,
    DeferredAnalysisWindowV1,
)
from leo_flow.deployments.gauss_staged_analysis_runtime import (
    _batch_affine_feature_projection_jobs,
)
from leo_flow.deployments.staged_analysis_pool import (
    BoundedSpawnDeferredAnalysisLaneV1,
)


def _window():
    return DeferredAnalysisWindowV1(
        Digest.sha256(b"definition"),
        0,
        tuple(CaptureBatchId(f"cbatch_pool_{index}") for index in range(36)),
        tuple(RecordingId(f"rec_pool_{index}") for index in range(72)),
        tuple(Digest.sha256(f"recording-{index}".encode()) for index in range(72)),
        tuple(JobId(f"job_pool_feature_{index}") for index in range(72)),
        tuple(JobId(f"job_pool_waterfall_{index}") for index in range(72)),
        tuple(JobId(f"job_pool_suite_{index}") for index in range(72)),
    )


@dataclass
class _Worker:
    def process_one(self, stage, window, worker_instance_id):
        del stage, window, worker_instance_id
        return False


class _States:
    def __init__(self, values):
        self.values = values

    def states(self, window, stage):
        ids = (
            window.feature_job_ids
            if stage.value.startswith("feature")
            else window.waterfall_job_ids
        )
        return {str(value): self.values for value in ids}


class _PendingThenSucceededStates(_States):
    def __init__(self):
        super().__init__("leased")
        self.inspections = 0

    def states(self, window, stage):
        self.inspections += 1
        self.values = "leased" if self.inspections == 1 else "succeeded"
        return super().states(window, stage)


@dataclass
class _EnvironmentWorker:
    def process_one(self, stage, window, worker_instance_id):
        del stage, window, worker_instance_id
        assert os.environ["OMP_NUM_THREADS"] == "1"
        assert "LEO_TEST_SECRET" not in os.environ
        return False


@dataclass
class _CrashWorker:
    def process_one(self, stage, window, worker_instance_id):
        del stage, window, worker_instance_id
        raise RuntimeError("private child failure")


def test_spawn_lane_reports_exact_complete_scope():
    lane = BoundedSpawnDeferredAnalysisLaneV1(_Worker(), _States("succeeded"))
    result = lane.drain(
        _window(),
        DeferredAnalysisStage.FEATURE_COMPUTE,
        workers=2,
        deadline_utc_ns=UtcNs(time.time_ns() + 10_000_000_000),
    )
    assert result.state is DeferredAnalysisLaneState.COMPLETE
    assert result.succeeded_count == 72


def test_spawn_lane_reports_parked_without_claiming_another_scope():
    lane = BoundedSpawnDeferredAnalysisLaneV1(_Worker(), _States("parked"))
    result = lane.drain(
        _window(),
        DeferredAnalysisStage.WATERFALL_PROJECTION,
        workers=2,
        deadline_utc_ns=UtcNs(time.time_ns() + 10_000_000_000),
    )
    assert result.state is DeferredAnalysisLaneState.PARKED
    assert len(result.parked_ids) == 72


def test_spawn_lane_waits_for_an_unexpired_durable_lease(monkeypatch):
    states = _PendingThenSucceededStates()
    sleeps = []
    monkeypatch.setattr(
        "leo_flow.deployments.staged_analysis_pool.time.sleep", sleeps.append
    )
    lane = BoundedSpawnDeferredAnalysisLaneV1(_Worker(), states)

    result = lane.drain(
        _window(),
        DeferredAnalysisStage.FEATURE_COMPUTE,
        workers=1,
        deadline_utc_ns=UtcNs(time.time_ns() + 10_000_000_000),
    )

    assert result.state is DeferredAnalysisLaneState.COMPLETE
    assert states.inspections == 2
    assert sleeps == [1.0]


def test_spawn_lane_retains_math_bound_but_scrubs_ambient_secret(monkeypatch):
    monkeypatch.setenv("OMP_NUM_THREADS", "1")
    monkeypatch.setenv("LEO_TEST_SECRET", "must-not-cross-child-boundary")
    lane = BoundedSpawnDeferredAnalysisLaneV1(
        _EnvironmentWorker(), _States("succeeded")
    )
    result = lane.drain(
        _window(),
        DeferredAnalysisStage.FEATURE_COMPUTE,
        workers=1,
        deadline_utc_ns=UtcNs(time.time_ns() + 10_000_000_000),
    )
    assert result.state is DeferredAnalysisLaneState.COMPLETE


def test_spawn_lane_fails_closed_and_reaps_all_children() -> None:
    before = {child.pid for child in multiprocessing.active_children()}
    lane = BoundedSpawnDeferredAnalysisLaneV1(_CrashWorker(), _States("ready"))

    with pytest.raises(RuntimeError, match="child failed"):
        lane.drain(
            _window(),
            DeferredAnalysisStage.FEATURE_COMPUTE,
            workers=2,
            deadline_utc_ns=UtcNs(time.time_ns() + 10_000_000_000),
        )

    assert {child.pid for child in multiprocessing.active_children()} == before


def test_spawn_lane_rejects_elapsed_deadline_before_spawning() -> None:
    lane = BoundedSpawnDeferredAnalysisLaneV1(_Worker(), _States("ready"))
    with pytest.raises(RuntimeError, match="deadline has elapsed"):
        lane.drain(
            _window(),
            DeferredAnalysisStage.FEATURE_COMPUTE,
            workers=1,
            deadline_utc_ns=UtcNs(time.time_ns() - 1),
        )


def test_feature_projection_shards_keep_recording_pairs_on_one_worker() -> None:
    window = _window()
    shards = tuple(
        _batch_affine_feature_projection_jobs(
            window, f"campaign-000-feature_projection-{index}-of-4"
        )
        for index in range(1, 5)
    )

    assert set().union(*(set(shard) for shard in shards)) == set(window.feature_job_ids)
    assert sum(len(shard) for shard in shards) == 72
    for offset in range(0, 72, 2):
        pair = set(window.feature_job_ids[offset : offset + 2])
        assert sum(pair.issubset(set(shard)) for shard in shards) == 1


@pytest.mark.parametrize(
    "worker_id",
    ["worker", "campaign-000-feature_projection-0-of-4", "x-1-of-5"],
)
def test_feature_projection_rejects_invalid_worker_shards(worker_id: str) -> None:
    with pytest.raises(RuntimeError, match="shard"):
        _batch_affine_feature_projection_jobs(_window(), worker_id)
