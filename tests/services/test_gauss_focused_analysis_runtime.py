from __future__ import annotations

import ast
import inspect

import pytest

from leo_flow.contracts.deferred_analysis import DeferredAnalysisStage
from leo_flow.deployments.gauss_focused_analysis_runtime import (
    MAXIMUM_FOCUSED_COMPUTE_WORKERS,
    focused_stage_worker_count,
)
from leo_flow.deployments.gauss_staged_analysis_runtime import _suite_compute


def test_focused_compute_policy_has_eight_worker_ceiling() -> None:
    assert MAXIMUM_FOCUSED_COMPUTE_WORKERS == 8
    assert focused_stage_worker_count(DeferredAnalysisStage.FEATURE_COMPUTE, 8) == 2
    assert focused_stage_worker_count(DeferredAnalysisStage.WATERFALL_COMPUTE, 8) == 2
    assert (
        focused_stage_worker_count(DeferredAnalysisStage.STARLINK_SUITE_COMPUTE, 8) == 2
    )


@pytest.mark.parametrize(
    "stage",
    [
        DeferredAnalysisStage.FEATURE_PROJECTION,
        DeferredAnalysisStage.WATERFALL_PROJECTION,
        DeferredAnalysisStage.STARLINK_SUITE_PROJECTION,
    ],
)
def test_focused_pair_serializes_projection(stage: DeferredAnalysisStage) -> None:
    assert focused_stage_worker_count(stage, 8) == 1


@pytest.mark.parametrize("workers", [0, 9])
def test_focused_compute_policy_rejects_out_of_bounds(workers: int) -> None:
    with pytest.raises(ValueError, match="within 1..8"):
        focused_stage_worker_count(DeferredAnalysisStage.FEATURE_COMPUTE, workers)


def test_focused_suite_compute_composes_temporal_pilot_preparers() -> None:
    tree = ast.parse(inspect.getsource(_suite_compute))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "CombinedStarlinkSuiteAnalysisJobPreparerV0_2"
    ]
    assert len(calls) == 1
    assert len(calls[0].args) == 5
    temporal = calls[0].args[-1]
    assert isinstance(temporal, ast.Call)
    assert isinstance(temporal.func, ast.Name)
    assert temporal.func.id == "starlink_temporal_pilot_preparers_v0_1"
