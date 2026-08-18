from __future__ import annotations

import ast
import inspect

import pytest

from leo_flow.adapters.hardware_link_postgres import (
    RecordingHardwareLinkConflictError,
)
from leo_flow.contracts.core import RecordingId
from leo_flow.contracts.deferred_analysis import DeferredAnalysisStage
from leo_flow.deployments import gauss_focused_analysis_runtime
from leo_flow.deployments.gauss_focused_analysis_runtime import (
    MAXIMUM_FOCUSED_COMPUTE_WORKERS,
    _link_focused_recording_hardware,
    focused_stage_worker_count,
)
from leo_flow.deployments.gauss_staged_analysis_runtime import _suite_compute
from leo_flow.hardware.persistence import (
    HardwareSnapshotIntegrityError,
    HardwareSnapshotNotFoundError,
)


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
    legacy_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "CombinedStarlinkSuiteAnalysisJobPreparerV0_2"
    ]
    assert len(legacy_calls) == 1
    assert len(legacy_calls[0].args) == 5
    temporal = legacy_calls[0].args[-1]
    assert isinstance(temporal, ast.Call)
    assert isinstance(temporal.func, ast.Name)
    assert temporal.func.id == "starlink_temporal_pilot_preparers_v0_1"

    dwell_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "CombinedStarlinkSuiteDwellAnalysisJobPreparerV0_3"
    ]
    assert len(dwell_calls) == 1
    assert legacy_calls[0] in dwell_calls[0].args
    profiles = dwell_calls[0].args[-1]
    assert isinstance(profiles, ast.Call)
    assert isinstance(profiles.func, ast.Name)
    assert profiles.func.id == "starlink_acquired_dwell_profiles_v0_3"
    window_budget = next(
        keyword.value
        for keyword in dwell_calls[0].keywords
        if keyword.arg == "maximum_windows_per_stream"
    )
    assert isinstance(window_budget, ast.Name)
    assert window_budget.id == "ACQUIRED_QAM_MAXIMUM_WINDOWS_PER_STREAM"


def test_focused_preparation_links_both_recordings_before_job_submission() -> None:
    class Linker:
        def __init__(self) -> None:
            self.recording_ids: list[RecordingId] = []

        def link(self, recording_id: RecordingId) -> object:
            self.recording_ids.append(recording_id)
            return object()

    linker = Linker()
    _link_focused_recording_hardware(
        (RecordingId("rec_z"), RecordingId("rec_a")), linker
    )

    assert linker.recording_ids == [RecordingId("rec_a"), RecordingId("rec_z")]


def test_missing_hardware_snapshot_does_not_block_focused_analysis(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Linker:
        def __init__(self) -> None:
            self.recording_ids: list[RecordingId] = []

        def link(self, recording_id: RecordingId) -> object:
            self.recording_ids.append(recording_id)
            if recording_id == RecordingId("rec_a"):
                raise HardwareSnapshotNotFoundError("snapshot is absent")
            return object()

    linker = Linker()
    _link_focused_recording_hardware(
        (RecordingId("rec_z"), RecordingId("rec_a")), linker
    )

    assert linker.recording_ids == [RecordingId("rec_a"), RecordingId("rec_z")]
    assert "continuing analysis without hardware linkage" in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        HardwareSnapshotIntegrityError("snapshot is corrupt"),
        RecordingHardwareLinkConflictError("link conflicts"),
    ],
)
def test_other_hardware_link_failures_still_block_focused_analysis(
    error: RuntimeError,
) -> None:
    class Linker:
        def link(self, recording_id: RecordingId) -> object:
            raise error

    with pytest.raises(type(error), match=str(error)):
        _link_focused_recording_hardware(
            (RecordingId("rec_a"), RecordingId("rec_z")), Linker()
        )


def test_focused_scope_invokes_authoritative_hardware_linkage_before_submission() -> (
    None
):
    source = inspect.getsource(gauss_focused_analysis_runtime._prepare_scope)
    assert source.index("_link_focused_recording_hardware") < source.index(
        "submitted = feature_submission.submit"
    )
