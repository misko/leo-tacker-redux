from __future__ import annotations

import multiprocessing
import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leo_flow.adapters.campaign_analysis_receipt_postgres import (
    FeatureProjectionReceiptEvidence,
)
from leo_flow.adapters.waterfall_receipt_postgres import WaterfallAnalysisReceiptV0_1
from leo_flow.application.capture_batch_dashboard import (
    initial_capture_batch_dashboard_view,
)
from leo_flow.capture.campaign import CampaignDefinition, build_campaign_unit
from leo_flow.capture.dual import CaptureAttemptRunResult
from leo_flow.capture.v5_station import load_v5_capture_station
from leo_flow.contracts.capture_batch import (
    CaptureAttemptOutcome,
    CaptureAttemptState,
    CaptureBatchDefinition,
    CaptureBatchMode,
    CaptureBatchSnapshot,
    ExpectedCaptureAttempt,
)
from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    CaptureAttemptId,
    CaptureBatchId,
    Digest,
    FeatureSetId,
    JobId,
    PlanId,
    RadioId,
    RecordingId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.dashboard_batch import DashboardAnalysisState
from leo_flow.contracts.features import FeatureSetBundle, FeatureSetRef
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)
from leo_flow.contracts.waterfall import WaterfallProductId, WaterfallProductRefV0_1
from leo_flow.deployments.gauss_campaign_runtime import (
    CampaignAnalysisError,
    CampaignCaptureError,
    ExactCampaignAnalysis,
    LinuxExternalRadioOwnershipGate,
    LocalCampaignCapacity,
    ProcessIsolatedCampaignCapture,
    StarlinkSuiteReceiptEvidenceV0_2,
    _run_analysis_children,
)
from leo_flow.jobs.contracts import JobSnapshot, JobState


def _overlap_analysis_child(
    connection: Any,
    active: Any,
    peak: Any,
    both_started: Any,
) -> None:
    with active.get_lock():
        active.value += 1
        peak.value = max(peak.value, active.value)
        if active.value == 2:
            both_started.set()
    progressed = both_started.wait(2.0)
    with active.get_lock():
        active.value -= 1
    connection.send(("ok", progressed))
    connection.close()


def _failing_analysis_child(
    connection: Any,
    active: Any,
    pids: Any,
    next_index: Any,
) -> None:
    with next_index.get_lock():
        index = next_index.value
        next_index.value += 1
        pids[index] = os.getpid()
    with active.get_lock():
        active.value += 1
    timeout = time.monotonic() + 2.0
    while active.value < 2 and time.monotonic() < timeout:
        time.sleep(0.005)
    if index == 0:
        connection.send(("error",))
        connection.close()
        return
    time.sleep(10.0)


def _late_analysis_child(connection: Any) -> None:
    time.sleep(10.0)


def _recording(suffix: str) -> PublishedRecordingRef:
    data = Digest.sha256(f"{suffix}:data".encode())
    metadata = Digest.sha256(f"{suffix}:metadata".encode())
    return PublishedRecordingRef(
        RecordingObjectRef(
            RecordingId(f"rec_{suffix}"),
            ObjectRef(data, 64, "application/octet-stream", "data-v1", f"cas:{data}"),
            ObjectRef(
                metadata,
                128,
                "application/json",
                "metadata-v1",
                f"cas:{metadata}",
            ),
            Digest.sha256(f"{suffix}:manifest".encode()),
        )
    )


def _snapshot() -> CaptureBatchSnapshot:
    definition = CaptureBatchDefinition(
        SchemaRef(CaptureBatchDefinition.SCHEMA_ID),
        CaptureBatchId("cbatch_campaign_analysis"),
        CaptureBatchMode.COORDINATED,
        (
            ExpectedCaptureAttempt(
                CaptureAttemptId("cattempt_campaign_a"),
                RadioId("radio_campaign_a"),
                PlanId("plan_campaign_a"),
                UtcNs(1_000),
            ),
            ExpectedCaptureAttempt(
                CaptureAttemptId("cattempt_campaign_b"),
                RadioId("radio_campaign_b"),
                PlanId("plan_campaign_b"),
                UtcNs(1_000),
            ),
        ),
        100,
    )
    outcomes = tuple(
        CaptureAttemptOutcome(
            SchemaRef(CaptureAttemptOutcome.SCHEMA_ID),
            definition.batch_id,
            attempt.attempt_id,
            attempt.radio_id,
            attempt.plan_id,
            CaptureAttemptState.SUCCEEDED,
            UtcNs(2_000 + index),
            UtcNs(1_500 + index),
            _recording(str(index)),
        )
        for index, attempt in enumerate(definition.expected_attempts)
    )
    return CaptureBatchSnapshot(
        SchemaRef(CaptureBatchSnapshot.SCHEMA_ID), definition, outcomes, 2
    )


class _Jobs:
    def __init__(self, snapshots: dict[JobId, JobSnapshot]) -> None:
        self.snapshots = snapshots

    def snapshot(self, job_id: JobId) -> JobSnapshot:
        return self.snapshots[job_id]


class _Projections:
    def __init__(self, receipts: dict[JobId, FeatureProjectionReceiptEvidence]) -> None:
        self.receipts = receipts

    def read(self, job_id: JobId) -> FeatureProjectionReceiptEvidence | None:
        return self.receipts.get(job_id)


class _Dashboard:
    def __init__(self, snapshot: CaptureBatchSnapshot) -> None:
        initial = initial_capture_batch_dashboard_view(snapshot)
        self.view = replace(
            initial,
            attempts=tuple(
                replace(
                    item,
                    analysis_state=DashboardAnalysisState.COMPLETE,
                    analysis_result_available=True,
                )
                for item in initial.attempts
            ),
        )

    def capture_batch(self, _batch_id: CaptureBatchId):
        return self.view


def _evidence(snapshot: CaptureBatchSnapshot):
    jobs: dict[JobId, JobSnapshot] = {}
    projections: dict[JobId, FeatureProjectionReceiptEvidence] = {}
    submitted = []
    for index, recording in enumerate(snapshot.successful_recordings):
        job_id = JobId(f"job_campaign_{index}")
        feature_digest = Digest.sha256(f"feature-{index}".encode())
        result = ArtifactRef(
            f"fset_campaign_{index}",
            feature_digest,
            SchemaRef(FeatureSetBundle.SCHEMA_ID),
        )
        jobs[job_id] = JobSnapshot(
            job_id, JobState.SUCCEEDED, 1, 1, result, None, None, None
        )
        feature_ref = FeatureSetRef(
            FeatureSetId(result.artifact_id),
            AnalysisRunId(f"arun_campaign_{index}"),
            ObjectRef(
                feature_digest,
                12,
                "application/json",
                "feature-v1",
                f"cas:{feature_digest}",
            ),
        )
        projections[job_id] = FeatureProjectionReceiptEvidence(
            f"fpwork_{'a' if index == 0 else 'b'}",
            job_id,
            "succeeded",
            feature_ref,
            recording.recording_id,
            recording.recording_object.identity_digest(),
            UtcNs(3_000 + index),
            "succeeded",
            result,
        )
        submitted.append(
            SimpleNamespace(
                job_id=job_id,
                request=SimpleNamespace(recording_id=recording.recording_id),
            )
        )
    return jobs, projections, tuple(submitted)


def test_exact_campaign_analysis_requires_jobs_projection_and_dashboard() -> None:
    snapshot = _snapshot()
    jobs, projections, submitted = _evidence(snapshot)
    port = ExactCampaignAnalysis(
        lambda _snapshot: SimpleNamespace(recording_jobs=submitted),
        _Jobs(jobs),
        _Projections(projections),
        _Dashboard(snapshot),
        lambda _deadline: False,
        lambda _deadline: False,
        now_utc_ns=lambda: 4_000,
    )

    receipt = port.analyze(snapshot, deadline_utc_ns=UtcNs(5_000))

    assert receipt.batch_id == snapshot.batch_id
    assert receipt.recording_ids == tuple(
        sorted((item.recording_id for item in snapshot.successful_recordings), key=str)
    )
    assert {item.analysis_job_id for item in receipt.successes} == set(jobs)


def test_exact_campaign_analysis_rejects_parked_exact_job() -> None:
    snapshot = _snapshot()
    jobs, projections, submitted = _evidence(snapshot)
    first = next(iter(jobs))
    jobs[first] = replace(
        jobs[first],
        state=JobState.PARKED,
        result_ref=None,
        park_reason="operator_parked",
        parked_at_utc_ns=UtcNs(3_000),
    )
    port = ExactCampaignAnalysis(
        lambda _snapshot: SimpleNamespace(recording_jobs=submitted),
        _Jobs(jobs),
        _Projections(projections),
        _Dashboard(snapshot),
        lambda _deadline: False,
        lambda _deadline: False,
        now_utc_ns=lambda: 4_000,
    )

    with pytest.raises(CampaignAnalysisError, match="parked"):
        port.analyze(snapshot, deadline_utc_ns=UtcNs(5_000))


def test_exact_campaign_analysis_proves_waterfalls_before_completion() -> None:
    snapshot = _snapshot()
    jobs, projections, submitted = _evidence(snapshot)
    waterfall_jobs: dict[RecordingId, JobId] = {}
    waterfall_receipts: dict[JobId, WaterfallAnalysisReceiptV0_1] = {}
    for index, recording in enumerate(snapshot.successful_recordings):
        job_id = JobId(f"job_campaign_waterfall_{index}")
        digest = Digest.sha256(f"waterfall-{index}".encode())
        product_id = WaterfallProductId("waterfall_" + f"{index + 1:x}" * 32)
        result = ArtifactRef(
            str(product_id), digest, SchemaRef("org.leo-flow.waterfall-bundle")
        )
        jobs[job_id] = JobSnapshot(
            job_id, JobState.SUCCEEDED, 1, 1, result, None, None, None
        )
        ref = WaterfallProductRefV0_1(
            product_id,
            AnalysisRunId("arun_" + f"{index + 3:x}" * 32),
            recording.recording_id,
            ObjectRef(
                digest,
                64,
                "application/json",
                "waterfall-bundle-v0.1",
                f"cas:{digest}",
            ),
        )
        waterfall_jobs[recording.recording_id] = job_id
        waterfall_receipts[job_id] = WaterfallAnalysisReceiptV0_1(
            "wfwork_" + f"{index + 5:x}" * 64,
            job_id,
            "succeeded",
            ref,
            recording.recording_object.identity_digest(),
            Digest.sha256(f"request-{index}".encode()),
            1,
            4,
            UtcNs(3_500 + index),
            "succeeded",
            result,
        )
    waterfall_reader = SimpleNamespace(
        read=lambda job_id: waterfall_receipts.get(job_id)
    )
    port = ExactCampaignAnalysis(
        lambda _snapshot: SimpleNamespace(recording_jobs=submitted),
        _Jobs(jobs),
        _Projections(projections),
        _Dashboard(snapshot),
        lambda _deadline: False,
        lambda _deadline: False,
        submit_waterfalls=lambda _snapshot: waterfall_jobs,
        waterfall_receipts=waterfall_reader,
        process_waterfall_one=lambda _deadline: False,
        project_waterfall_one=lambda _deadline: False,
        now_utc_ns=lambda: 4_000,
    )

    receipt = port.analyze(snapshot, deadline_utc_ns=UtcNs(5_000))

    assert receipt.recording_ids == tuple(sorted(waterfall_jobs, key=str))


def test_exact_campaign_analysis_rejects_empty_configured_waterfall_submission() -> (
    None
):
    snapshot = _snapshot()
    jobs, projections, submitted = _evidence(snapshot)
    port = ExactCampaignAnalysis(
        lambda _snapshot: SimpleNamespace(recording_jobs=submitted),
        _Jobs(jobs),
        _Projections(projections),
        _Dashboard(snapshot),
        lambda _deadline: False,
        lambda _deadline: False,
        submit_waterfalls=lambda _snapshot: {},
        waterfall_receipts=SimpleNamespace(read=lambda _job_id: None),
        process_waterfall_one=lambda _deadline: False,
        project_waterfall_one=lambda _deadline: False,
        now_utc_ns=lambda: 4_000,
    )

    with pytest.raises(CampaignAnalysisError, match="two exact waterfall jobs"):
        port.analyze(snapshot, deadline_utc_ns=UtcNs(5_000))


def test_exact_campaign_analysis_requires_terminal_detector_suite_for_both_recordings() -> (
    None
):
    snapshot = _snapshot()
    jobs, projections, submitted = _evidence(snapshot)
    suite_jobs: dict[RecordingId, JobId] = {}
    receipts: dict[JobId, StarlinkSuiteReceiptEvidenceV0_2] = {}
    for index, recording in enumerate(snapshot.successful_recordings):
        job_id = JobId(f"job_campaign_suite_{index}")
        result = ArtifactRef(
            "slsuite_" + f"{index + 1:x}" * 32,
            Digest.sha256(f"suite-{index}".encode()),
            SchemaRef("org.leo-flow.starlink-detector-suite-recording-bundle"),
        )
        jobs[job_id] = JobSnapshot(
            job_id, JobState.SUCCEEDED, 1, 1, result, None, None, None
        )
        suite_jobs[recording.recording_id] = job_id
        receipts[job_id] = StarlinkSuiteReceiptEvidenceV0_2(
            "slsuitework_" + f"{index + 2:x}" * 64,
            job_id,
            "succeeded",
            recording.recording_id,
            "candidates",
            16,
            128,
            UtcNs(3_700 + index),
        )
    port = ExactCampaignAnalysis(
        lambda _snapshot: SimpleNamespace(recording_jobs=submitted),
        _Jobs(jobs),
        _Projections(projections),
        _Dashboard(snapshot),
        lambda _deadline: False,
        lambda _deadline: False,
        submit_starlink_suites=lambda _snapshot: suite_jobs,
        starlink_suite_receipts=SimpleNamespace(
            read=lambda job_id: receipts.get(job_id)
        ),
        process_starlink_suites=lambda _count, _deadline: False,
        project_starlink_suite_one=lambda _deadline: False,
        now_utc_ns=lambda: 4_000,
    )
    receipt = port.analyze(snapshot, deadline_utc_ns=UtcNs(5_000))
    assert receipt.recording_ids == tuple(sorted(suite_jobs, key=str))


def test_exact_campaign_analysis_rejects_incomplete_detector_method_closure() -> None:
    snapshot = _snapshot()
    jobs, projections, submitted = _evidence(snapshot)
    suite_jobs = {
        recording.recording_id: JobId(f"job_suite_bad_{index}")
        for index, recording in enumerate(snapshot.successful_recordings)
    }
    for job_id in suite_jobs.values():
        jobs[job_id] = JobSnapshot(
            job_id,
            JobState.SUCCEEDED,
            1,
            1,
            ArtifactRef(
                "slsuite_" + "a" * 32,
                Digest.sha256(str(job_id).encode()),
                SchemaRef("org.leo-flow.starlink-detector-suite-recording-bundle"),
            ),
            None,
            None,
            None,
        )
    reader = SimpleNamespace(
        read=lambda job_id: StarlinkSuiteReceiptEvidenceV0_2(
            "slsuitework_" + "b" * 64,
            job_id,
            "succeeded",
            next(
                recording_id
                for recording_id, candidate in suite_jobs.items()
                if candidate == job_id
            ),
            "candidates",
            16,
            127,
            UtcNs(3_900),
        )
    )
    port = ExactCampaignAnalysis(
        lambda _snapshot: SimpleNamespace(recording_jobs=submitted),
        _Jobs(jobs),
        _Projections(projections),
        _Dashboard(snapshot),
        lambda _deadline: False,
        lambda _deadline: False,
        submit_starlink_suites=lambda _snapshot: suite_jobs,
        starlink_suite_receipts=reader,
        process_starlink_suites=lambda _count, _deadline: False,
        project_starlink_suite_one=lambda _deadline: False,
        now_utc_ns=lambda: 4_000,
    )
    with pytest.raises(CampaignAnalysisError, match="method closure"):
        port.analyze(snapshot, deadline_utc_ns=UtcNs(5_000))


def test_exact_campaign_analysis_rejects_detector_suite_receipt_identity_drift() -> (
    None
):
    snapshot = _snapshot()
    jobs, projections, submitted = _evidence(snapshot)
    suite_jobs = {
        recording.recording_id: JobId(f"job_suite_identity_{index}")
        for index, recording in enumerate(snapshot.successful_recordings)
    }
    receipts: dict[JobId, StarlinkSuiteReceiptEvidenceV0_2] = {}
    for index, (recording_id, job_id) in enumerate(suite_jobs.items()):
        jobs[job_id] = JobSnapshot(
            job_id,
            JobState.SUCCEEDED,
            1,
            1,
            ArtifactRef(
                "slsuite_" + f"{index + 1:x}" * 32,
                Digest.sha256(str(job_id).encode()),
                SchemaRef("org.leo-flow.starlink-detector-suite-recording-bundle"),
            ),
            None,
            None,
            None,
        )
        receipts[job_id] = StarlinkSuiteReceiptEvidenceV0_2(
            "slsuitework_" + f"{index + 3:x}" * 64,
            job_id,
            "succeeded",
            recording_id,
            "candidates",
            16,
            128,
            UtcNs(3_800 + index),
        )
    first_job = next(iter(suite_jobs.values()))
    receipts[first_job] = replace(
        receipts[first_job], recording_id=RecordingId("rec_wrong_identity")
    )
    port = ExactCampaignAnalysis(
        lambda _snapshot: SimpleNamespace(recording_jobs=submitted),
        _Jobs(jobs),
        _Projections(projections),
        _Dashboard(snapshot),
        lambda _deadline: False,
        lambda _deadline: False,
        submit_starlink_suites=lambda _snapshot: suite_jobs,
        starlink_suite_receipts=SimpleNamespace(
            read=lambda job_id: receipts.get(job_id)
        ),
        process_starlink_suites=lambda _count, _deadline: False,
        project_starlink_suite_one=lambda _deadline: False,
        now_utc_ns=lambda: 4_000,
    )

    with pytest.raises(CampaignAnalysisError, match="receipt identity differs"):
        port.analyze(snapshot, deadline_utc_ns=UtcNs(5_000))


def test_exact_campaign_analysis_processes_both_suites_together_then_projects_exactly_two() -> (
    None
):
    snapshot = _snapshot()
    jobs, projections, submitted = _evidence(snapshot)
    suite_jobs = {
        recording.recording_id: JobId(f"job_suite_concurrent_{index}")
        for index, recording in enumerate(snapshot.successful_recordings)
    }
    for job_id in suite_jobs.values():
        jobs[job_id] = JobSnapshot(job_id, JobState.READY, 0, 1, None, None, None, None)
    receipts: dict[JobId, StarlinkSuiteReceiptEvidenceV0_2] = {}
    process_counts: list[int] = []
    projected: list[JobId] = []

    def process_suites(count: int, _deadline: UtcNs) -> bool:
        process_counts.append(count)
        assert count == 2
        for index, (recording_id, job_id) in enumerate(suite_jobs.items()):
            result = ArtifactRef(
                "slsuite_" + f"{index + 7:x}" * 32,
                Digest.sha256(str(job_id).encode()),
                SchemaRef("org.leo-flow.starlink-detector-suite-recording-bundle"),
            )
            jobs[job_id] = JobSnapshot(
                job_id, JobState.SUCCEEDED, 1, 1, result, None, None, None
            )
            receipts[job_id] = StarlinkSuiteReceiptEvidenceV0_2(
                "slsuitework_" + f"{index + 7:x}" * 64,
                job_id,
                "ready",
                recording_id,
                "candidates",
                16,
                128,
                None,
            )
        return True

    def project_one(_deadline: UtcNs) -> bool:
        job_id = next(job for job in suite_jobs.values() if job not in projected)
        projected.append(job_id)
        receipts[job_id] = replace(
            receipts[job_id], work_state="succeeded", projected_utc_ns=UtcNs(3_900)
        )
        return True

    port = ExactCampaignAnalysis(
        lambda _snapshot: SimpleNamespace(recording_jobs=submitted),
        _Jobs(jobs),
        _Projections(projections),
        _Dashboard(snapshot),
        lambda _deadline: False,
        lambda _deadline: False,
        submit_starlink_suites=lambda _snapshot: suite_jobs,
        starlink_suite_receipts=SimpleNamespace(
            read=lambda job_id: receipts.get(job_id)
        ),
        process_starlink_suites=process_suites,
        project_starlink_suite_one=project_one,
        now_utc_ns=lambda: 4_000,
    )

    receipt = port.analyze(snapshot, deadline_utc_ns=UtcNs(5_000))

    assert process_counts == [2]
    assert projected == list(suite_jobs.values())
    assert receipt.recording_ids == tuple(sorted(suite_jobs, key=str))


def test_exact_campaign_analysis_never_completes_with_one_suite_unfinished() -> None:
    snapshot = _snapshot()
    jobs, projections, submitted = _evidence(snapshot)
    suite_jobs = {
        recording.recording_id: JobId(f"job_suite_partial_{index}")
        for index, recording in enumerate(snapshot.successful_recordings)
    }
    for job_id in suite_jobs.values():
        jobs[job_id] = JobSnapshot(job_id, JobState.READY, 0, 1, None, None, None, None)
    process_counts: list[int] = []
    clock = 4_000

    def process_suites(count: int, _deadline: UtcNs) -> bool:
        process_counts.append(count)
        if len(process_counts) == 1:
            job_id = next(iter(suite_jobs.values()))
            jobs[job_id] = JobSnapshot(
                job_id,
                JobState.SUCCEEDED,
                1,
                1,
                ArtifactRef(
                    "slsuite_" + "d" * 32,
                    Digest.sha256(str(job_id).encode()),
                    SchemaRef("org.leo-flow.starlink-detector-suite-recording-bundle"),
                ),
                None,
                None,
                None,
            )
            return True
        return False

    def now() -> int:
        nonlocal clock
        clock += 100
        return clock

    port = ExactCampaignAnalysis(
        lambda _snapshot: SimpleNamespace(recording_jobs=submitted),
        _Jobs(jobs),
        _Projections(projections),
        _Dashboard(snapshot),
        lambda _deadline: False,
        lambda _deadline: False,
        submit_starlink_suites=lambda _snapshot: suite_jobs,
        starlink_suite_receipts=SimpleNamespace(read=lambda _job_id: None),
        process_starlink_suites=process_suites,
        project_starlink_suite_one=lambda _deadline: False,
        now_utc_ns=now,
        delay=lambda _seconds: None,
    )

    with pytest.raises(CampaignAnalysisError, match="absolute deadline"):
        port.analyze(snapshot, deadline_utc_ns=UtcNs(5_000))
    assert process_counts[0] == 2
    assert process_counts[1:]
    assert set(process_counts[1:]) == {1}


@pytest.mark.parametrize("child_count", [0, 3])
def test_analysis_children_reject_out_of_bound_worker_count(child_count: int) -> None:
    with pytest.raises(CampaignAnalysisError, match="within 1..2"):
        _run_analysis_children(
            "bounds-test",
            SimpleNamespace(),  # type: ignore[arg-type]
            Path("/unused"),
            UtcNs(time.time_ns() + 1_000_000_000),
            child_count=child_count,
            child_target=_late_analysis_child,
            child_target_args=(),
        )


def test_analysis_children_overlap_in_separate_processes() -> None:
    context = multiprocessing.get_context("spawn")
    active = context.Value("i", 0)
    peak = context.Value("i", 0)
    both_started = context.Event()

    progressed = _run_analysis_children(
        "overlap-test",
        SimpleNamespace(),  # type: ignore[arg-type]
        Path("/unused"),
        UtcNs(time.time_ns() + 5_000_000_000),
        child_count=2,
        child_target=_overlap_analysis_child,
        child_target_args=(active, peak, both_started),
    )

    assert progressed
    assert peak.value == 2
    assert active.value == 0


def test_analysis_child_failure_cancels_and_reaps_peer() -> None:
    context = multiprocessing.get_context("spawn")
    active = context.Value("i", 0)
    pids = context.Array("i", 2)
    next_index = context.Value("i", 0)

    with pytest.raises(CampaignAnalysisError, match="failed"):
        _run_analysis_children(
            "failure-test",
            SimpleNamespace(),  # type: ignore[arg-type]
            Path("/unused"),
            UtcNs(time.time_ns() + 5_000_000_000),
            child_count=2,
            child_target=_failing_analysis_child,
            child_target_args=(active, pids, next_index),
        )

    assert all(pid > 0 for pid in pids)
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_analysis_children_deadline_includes_bounded_peer_cleanup() -> None:
    started = time.monotonic()
    with pytest.raises(CampaignAnalysisError, match="absolute deadline"):
        _run_analysis_children(
            "deadline-test",
            SimpleNamespace(),  # type: ignore[arg-type]
            Path("/unused"),
            UtcNs(time.time_ns() + 200_000_000),
            child_count=2,
            child_target=_late_analysis_child,
            child_target_args=(),
        )

    assert time.monotonic() - started < 1.0


def test_local_campaign_capacity_uses_lower_available_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cas = tmp_path / "cas"
    staging = tmp_path / "staging"
    cas.mkdir()
    staging.mkdir()
    values = {
        cas: SimpleNamespace(f_bavail=100, f_frsize=4),
        staging: SimpleNamespace(f_bavail=50, f_frsize=4),
    }
    monkeypatch.setattr("os.statvfs", lambda path: values[path])

    assert LocalCampaignCapacity(cas, staging).available_bytes() == 200


def test_linux_ownership_gate_rejects_process_naming_exact_radio(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "net" / "tcp").write_text(
        "sl local_address rem_address st\n", encoding="ascii"
    )
    process = proc / "123"
    process.mkdir()
    (process / "cmdline").write_bytes(b"plutod\0--iio-ip\0" + b"192.168.1.15\0")
    gate = LinuxExternalRadioOwnershipGate(
        ("192.168.1.20", "192.168.1.21"), proc_root=proc
    )
    gate.require_clear()

    (process / "cmdline").write_bytes(b"plutod\0--iio-ip\0" + b"192.168.1.20\0")
    with pytest.raises(Exception, match="external process"):
        gate.require_clear()


def test_process_isolated_campaign_capture_composes_exact_terminal_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import leo_flow.deployments.gauss_campaign_runtime as runtime

    first = load_v5_capture_station(
        Path("deploy/v5-scan/gauss-radio-20-pluto-5d4d.station.json")
    )
    second = load_v5_capture_station(
        Path("deploy/v5-scan/gauss-radio-21-pluto-19f2.station.json")
    )
    now = 2_000_000_000_000
    definition = CampaignDefinition(
        "qual_runtime_test",
        UtcNs(now),
        first.radio.radio_id,
        second.radio.radio_id,
        first.specification_digest,
        second.specification_digest,
        maximum_start_lateness_ns=5_000_000_000,
        qualification=True,
    )
    unit = build_campaign_unit(
        definition,
        success_index=0,
        slot_index=0,
        retry_index=0,
        requested_start_utc_ns=UtcNs(now),
    )

    class Ownership:
        calls = 0

        def require_clear(self) -> None:
            self.calls += 1

    class Lock:
        released = False

        def __init__(self, _path: Path) -> None:
            pass

        def acquire(self) -> None:
            pass

        def release(self) -> None:
            Lock.released = True

    class Supervisor:
        def abort_all(self) -> None:
            pass

    class Provider:
        def __init__(self, _path: Path) -> None:
            pass

        def resolve(self, _name: str) -> str:
            return "test-dsn"

    class Gate:
        def ready(self) -> bool:
            return True

    projected_views: list[object] = []

    class Writer:
        def publish(self, view):
            projected_views.append(view)
            return 1

    dispatch_delays: list[float] = []

    def runner_builder(
        station,
        _credential,
        batch_id,
        _supervisor,
        *,
        post_release_dispatch_delay_s=0.0,
    ):
        dispatch_delays.append(post_release_dispatch_delay_s)

        class Runner:
            def run(self, attempt, control):
                assert control.ready_and_wait_for_release()
                return CaptureAttemptRunResult(
                    SchemaRef(CaptureAttemptRunResult.SCHEMA_ID),
                    batch_id,
                    attempt.attempt_id,
                    attempt.radio_id,
                    attempt.plan_id,
                    UtcNs(now + (0 if station is not None else 1)),
                    UtcNs(now + 100),
                    _recording(str(attempt.attempt_id)),
                )

        return Runner()

    monkeypatch.setattr(runtime, "ExclusiveModeLock", Lock)
    monkeypatch.setattr(runtime, "SpawnProcessSupervisor", Supervisor)
    monkeypatch.setattr(runtime, "SystemdCredentialProvider", Provider)
    monkeypatch.setattr(runtime, "_postgres_drain_gate", lambda _dsn: Gate())
    monkeypatch.setattr(runtime, "_projection_writer", lambda _dsn: Writer())
    monkeypatch.setattr(runtime, "_build_process_isolated_runner", runner_builder)
    ownership = Ownership()
    state_root = tmp_path / "campaign"
    state_root.mkdir()
    capture = ProcessIsolatedCampaignCapture(
        definition,
        first,
        second,
        state_root,
        state_root / "batches.sqlite3",
        tmp_path / "credentials",
        ownership,
        secondary_dispatch_delay_s=0.01,
        now_utc_ns=lambda: now,
    )

    state = capture.capture(
        unit,
        not_before_utc_ns=UtcNs(now),
        deadline_utc_ns=UtcNs(now + 1_000_000_000),
    )

    assert state.terminal
    assert all(item.state is CaptureAttemptState.SUCCEEDED for item in state.outcomes)
    assert ownership.calls == 3
    assert dispatch_delays == [0.0, 0.01]
    assert Lock.released
    assert len(projected_views) == 1

    class ClosedGate:
        def ready(self) -> bool:
            return False

    blocked = ProcessIsolatedCampaignCapture(
        definition,
        first,
        second,
        state_root,
        state_root / "batches.sqlite3",
        tmp_path / "credentials",
        ownership,
        admission_builder=lambda _dsn: ClosedGate(),
        secondary_dispatch_delay_s=0.01,
        now_utc_ns=lambda: now,
    )
    with pytest.raises(CampaignCaptureError, match="admission gate is closed"):
        blocked.capture(
            unit,
            not_before_utc_ns=UtcNs(now),
            deadline_utc_ns=UtcNs(now + 1_000_000_000),
        )
