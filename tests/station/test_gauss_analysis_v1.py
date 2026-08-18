from __future__ import annotations

import inspect
import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from leo_flow.contracts.capture_batch import (
    CaptureAttemptOutcome,
    CaptureAttemptState,
    CaptureBatchDefinition,
    CaptureBatchMode,
    CaptureBatchSnapshot,
    ExpectedCaptureAttempt,
)
from leo_flow.contracts.capture_batch_codec import encode_capture_batch_snapshot
from leo_flow.contracts.core import (
    CaptureAttemptId,
    CaptureBatchId,
    Digest,
    JobId,
    PlanId,
    RadioId,
    RecordingId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)
from leo_flow.deployments.offline_analysis_v1 import AlgorithmKey
from leo_flow.jobs.memory import InMemoryJobLeaseRepository
from leo_flow.services import Process
from leo_flow.services.capture_batch_analysis import SubmittedClosedBatchAnalysis
from leo_flow.services.config import AnalysisServiceConfig
from leo_flow.services.recording_submission import SubmittedRecordingAnalysis
from leo_flow.services.recording_submission_operator import (
    RecordingSubmissionOperatorConfig,
)
from leo_station import analysis_operator, analysis_v1

ROOT = Path(__file__).parents[2]
DEPLOYMENT = ROOT / "deploy" / "gauss-analysis-v1"


def test_analysis_help_names_installed_cli_and_describes_offline_boundary() -> None:
    help_text = " ".join(analysis_operator._parser().format_help().split())

    assert help_text.startswith("usage: leo-gauss-analysis")
    assert (
        "validate checked science and service config without credentials, "
        "database, CAS, or radio I/O"
    ) in help_text


class _ModeLock:
    def __init__(self, calls: list[str], *, fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail

    def acquire(self) -> None:
        self.calls.append("acquire")
        if self.fail:
            raise RuntimeError("contended")

    def release(self) -> None:
        self.calls.append("release")


def _recording(name: str) -> PublishedRecordingRef:
    data = Digest.sha256(f"{name}:data".encode())
    metadata = Digest.sha256(f"{name}:metadata".encode())
    return PublishedRecordingRef(
        RecordingObjectRef(
            RecordingId(f"rec_{name}"),
            ObjectRef(
                data,
                10,
                "application/octet-stream",
                "recording-data-v1",
                f"cas:sha256:{data.value}",
            ),
            ObjectRef(
                metadata,
                20,
                "application/json",
                "recording-metadata-v1",
                f"cas:sha256:{metadata.value}",
            ),
            Digest.sha256(f"{name}:manifest".encode()),
        )
    )


def _terminal_batch() -> CaptureBatchSnapshot:
    definition = CaptureBatchDefinition(
        SchemaRef(CaptureBatchDefinition.SCHEMA_ID),
        CaptureBatchId("cbatch_gauss_operator"),
        CaptureBatchMode.INDEPENDENT,
        (
            ExpectedCaptureAttempt(
                CaptureAttemptId("cattempt_gauss_a"),
                RadioId("radio_gauss_a"),
                PlanId("plan_gauss_a"),
                UtcNs(1_000),
            ),
            ExpectedCaptureAttempt(
                CaptureAttemptId("cattempt_gauss_b"),
                RadioId("radio_gauss_b"),
                PlanId("plan_gauss_b"),
                UtcNs(2_000),
            ),
        ),
    )
    outcomes = tuple(
        CaptureAttemptOutcome(
            SchemaRef(CaptureAttemptOutcome.SCHEMA_ID),
            definition.batch_id,
            attempt.attempt_id,
            attempt.radio_id,
            attempt.plan_id,
            CaptureAttemptState.SUCCEEDED,
            UtcNs(3_100 + index),
            UtcNs(3_000 + index),
            _recording(str(attempt.attempt_id)),
        )
        for index, attempt in enumerate(definition.expected_attempts)
    )
    return CaptureBatchSnapshot(
        SchemaRef(CaptureBatchSnapshot.SCHEMA_ID), definition, outcomes, 2
    )


def _submitted_batch(snapshot: CaptureBatchSnapshot) -> SubmittedClosedBatchAnalysis:
    jobs = tuple(
        cast(
            SubmittedRecordingAnalysis,
            SimpleNamespace(
                job_id=JobId(f"job_{index}"),
                request=SimpleNamespace(recording_id=recording.recording_id),
            ),
        )
        for index, recording in enumerate(snapshot.successful_recordings)
    )
    return SubmittedClosedBatchAnalysis(
        snapshot,
        snapshot.paired_analysis_eligibility,
        jobs,
    )


def test_checked_manifest_and_config_match_the_imported_plugin() -> None:
    manifest = analysis_operator.load_approved_manifest(DEPLOYMENT / "science.json")
    config = analysis_operator._load_analysis_config(DEPLOYMENT / "analysis.json")

    assert manifest == analysis_v1.science_manifest()
    assert isinstance(config, AnalysisServiceConfig)
    assert config.runtime.instance_id == "gauss-offline-analysis-1"
    assert set(analysis_v1.PLUGIN.builders) == {Process.ANALYSIS}
    assert set(analysis_v1.SCIENTIFIC.recording_analyzers) == {
        AlgorithmKey(
            analysis_v1.RECORDING_ALGORITHM_REF,
            analysis_v1.RECORDING_CONFIG_REF,
        )
    }
    assert Digest.sha256((ROOT / "uv.lock").read_bytes()) == (
        analysis_v1.DEPENDENCY_LOCK_REF.digest
    )
    assert analysis_v1.CAS_ROOT == Path("/home/mouse9911/.local/share/leo-flow/objects")
    assert analysis_v1.MODE_LOCK_PATH == Path(
        "/home/mouse9911/.local/state/leo-flow/pipeline-mode.lock"
    )


def test_starlink_profiles_are_exact_rate_specific_and_equally_bounded() -> None:
    profiles = analysis_v1.STARLINK_SEARCH_PROFILES
    assert tuple(profile.sample_rate_hz for profile in profiles) == (
        2_500_000.0,
        5_000_000.0,
    )
    assert tuple(profile.probe_sample_count for profile in profiles) == (
        20_000,
        40_000,
    )
    assert {
        profile.probe_sample_count / profile.sample_rate_hz for profile in profiles
    } == {0.008}
    assert {profile.config.search_cell_count for profile in profiles} == {583}
    assert len({profile.config_ref for profile in profiles}) == 2
    assert profiles[0].config.cfo_hypotheses_hz == (
        profiles[1].config.cfo_hypotheses_hz
    )
    assert tuple(
        len(profile.config.epoch_hypotheses_samples) for profile in profiles
    ) == (
        53,
        53,
    )
    assert tuple(
        profile.config.epoch_hypotheses_samples[1] / profile.sample_rate_hz
        for profile in profiles
    ) == (64 / 2_500_000, 128 / 5_000_000)

    with pytest.raises(ValueError, match="no approved Qin search profile"):
        analysis_v1.starlink_search_profile_v0_1(1_250_000.0)


def test_science_manifest_pins_rate_specific_qin_template_digests() -> None:
    profiles = analysis_v1.science_manifest()["starlink_candidates"]["profiles"]
    assert tuple(profile["sample_rate_hz"] for profile in profiles) == (
        2_500_000.0,
        5_000_000.0,
    )
    assert profiles[0]["template_refs"]["lower"]["exact"]["digest"]["value"] == (
        "53b5bb1d72349c5038adad7a9a8944f3b7aa9174db0ce026ad09761d4e91d929"
    )
    assert profiles[1]["template_refs"]["lower"]["exact"]["digest"]["value"] == (
        "b43874f94533fae9914d586e5cd33ce8507a775d47396c877e3fbede06ef5112"
    )
    assert profiles[0]["config_ref"] != profiles[1]["config_ref"]


def test_science_manifest_pins_stratified_temporal_search_resources() -> None:
    temporal = analysis_v1.science_manifest()["starlink_temporal_pilot"]

    assert temporal == {
        "source": "detector-suite-v0.2-exact-search-grid",
        "window_duration_ms": 8,
        "nominal_stride_seconds": 5,
        "maximum_probe_count": 8,
        "surrogate_count": 4,
        "coverage_semantics": "stratified-temporal-sampling-not-full-dwell",
        "decision_semantics": "candidate-evidence-not-calibrated-detection",
        "requested_output_schema": {
            "schema_id": "org.leo-flow.starlink-temporal-pilot-recording-bundle",
            "version": "0.1",
        },
    }


def test_acquired_qam_profiles_cover_every_eligible_gauss_receiver() -> None:
    profiles = analysis_v1.starlink_acquired_dwell_profiles_v0_3()

    assert len(profiles) == 8
    assert {str(profile.receiver_chain_id) for profile in profiles} == {
        "rx_lnb_a",
        "rx_lnb_b",
        "rx_lnb_c",
        "rx_lnb_d",
    }
    assert len(
        {(profile.suite_config_ref, profile.receiver_chain_id) for profile in profiles}
    ) == len(profiles)


def test_plugin_imports_no_capture_radio_or_private_storage_paths() -> None:
    source = inspect.getsource(analysis_v1) + inspect.getsource(analysis_operator)
    forbidden = (
        "leo_flow.capture",
        "RadioDevice",
        "capture-spool",
        "recordings/",
        "glob(",
        "rglob(",
    )
    assert not [item for item in forbidden if item in source]


def test_dependency_approval_fails_before_recording_access() -> None:
    analyzer = analysis_v1.SCIENTIFIC.recording_analyzers[
        AlgorithmKey(
            analysis_v1.RECORDING_ALGORITHM_REF,
            analysis_v1.RECORDING_CONFIG_REF,
        )
    ]
    request = cast(
        object,
        SimpleNamespace(dependency_refs=()),
    )
    with pytest.raises(ValueError, match="dependency refs differ"):
        analyzer.analyze(cast(object, None), request)  # type: ignore[arg-type]


def test_science_runtime_rejects_a_different_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(analysis_v1.sys, "version_info", (3, 14, 0))
    with pytest.raises(RuntimeError, match="exact Python 3.11.16"):
        analysis_v1.require_approved_runtime()


def test_manifest_rejects_changes_duplicates_and_oversize(tmp_path: Path) -> None:
    changed = analysis_v1.science_manifest()
    changed["plugin_id"] = "latest"
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(analysis_operator.GaussAnalysisOperatorError, match="differs"):
        analysis_operator.load_approved_manifest(path)

    path.write_text('{"schema":"one","schema":"two"}', encoding="utf-8")
    with pytest.raises(analysis_operator.GaussAnalysisOperatorError, match="duplicate"):
        analysis_operator.load_approved_manifest(path)

    path.write_bytes(b" " * (analysis_operator.MAX_MANIFEST_BYTES + 1))
    with pytest.raises(analysis_operator.GaussAnalysisOperatorError, match="size"):
        analysis_operator.load_approved_manifest(path)


def test_validate_is_machine_readable_and_performs_no_operator_io() -> None:
    stdout, stderr = StringIO(), StringIO()

    result = analysis_operator.main(
        [
            "validate",
            "--config",
            str(DEPLOYMENT / "analysis.json"),
            "--science-manifest",
            str(DEPLOYMENT / "science.json"),
        ],
        stdout=stdout,
        stderr=stderr,
        submitter=lambda config, credentials: pytest.fail("submission called"),
        processor=lambda config, credentials, output: pytest.fail("processor called"),
        projector=lambda credentials: pytest.fail("projector called"),
    )

    assert result == analysis_operator.ExitCode.OK
    assert json.loads(stdout.getvalue()) == {
        "event": "gauss_analysis_valid",
        "instance_id": "gauss-offline-analysis-1",
        "science_manifest_digest": str(analysis_v1.SCIENCE_MANIFEST_DIGEST),
        "source_commit": analysis_v1.SOURCE_COMMIT,
    }
    assert stderr.getvalue() == ""


def test_submit_builds_only_the_checked_exact_selection(tmp_path: Path) -> None:
    captured: list[RecordingSubmissionOperatorConfig] = []
    lock_calls: list[str] = []

    def submitter(config, credentials):
        del credentials
        captured.append(config)
        request = SimpleNamespace(recording_id=RecordingId("rec_after_capture"))
        return cast(
            SubmittedRecordingAnalysis,
            SimpleNamespace(job_id=JobId("job_exact_analysis"), request=request),
        )

    stdout, stderr = StringIO(), StringIO()
    result = analysis_operator.main(
        [
            "submit",
            "--recording-id",
            "rec_after_capture",
            "--science-manifest",
            str(DEPLOYMENT / "science.json"),
            "--credential-directory",
            str(tmp_path),
        ],
        stdout=stdout,
        stderr=stderr,
        submitter=submitter,
        mode_lock_factory=lambda path: _ModeLock(lock_calls),
    )

    assert result == analysis_operator.ExitCode.OK
    assert len(captured) == 1
    selection = captured[0].selection
    assert selection.recording_id == RecordingId("rec_after_capture")
    assert selection.algorithm_ref == analysis_v1.RECORDING_ALGORITHM_REF
    assert selection.config_ref == analysis_v1.RECORDING_CONFIG_REF
    assert selection.dependency_refs == analysis_v1.RECORDING_DEPENDENCY_REFS
    assert json.loads(stdout.getvalue())["event"] == "gauss_analysis_submitted"
    assert stderr.getvalue() == ""
    assert lock_calls == ["acquire", "release"]


def test_submit_starlink_reports_candidate_semantics_under_mode_lock(
    tmp_path: Path,
) -> None:
    lock_calls: list[str] = []

    def submitter(recording_id, credentials):
        del credentials
        return SimpleNamespace(
            job_id=JobId("job_starlink_exact"),
            request=SimpleNamespace(recording_id=recording_id),
        )

    stdout, stderr = StringIO(), StringIO()
    result = analysis_operator.main(
        [
            "submit-starlink",
            "--recording-id",
            "rec_after_capture",
            "--science-manifest",
            str(DEPLOYMENT / "science.json"),
            "--credential-directory",
            str(tmp_path),
        ],
        stdout=stdout,
        stderr=stderr,
        starlink_submitter=submitter,
        mode_lock_factory=lambda path: _ModeLock(lock_calls),
    )

    assert result == analysis_operator.ExitCode.OK
    assert json.loads(stdout.getvalue()) == {
        "event": "gauss_starlink_submitted",
        "job_id": "job_starlink_exact",
        "recording_id": "rec_after_capture",
        "candidate_semantics": "uncalibrated-search-only",
        "science_manifest_digest": str(analysis_v1.SCIENCE_MANIFEST_DIGEST),
    }
    assert stderr.getvalue() == ""
    assert lock_calls == ["acquire", "release"]


def test_submit_starlink_suite_exposes_exact_recording_backfill_command(
    tmp_path: Path,
) -> None:
    lock_calls: list[str] = []

    def submitter(recording_id, credentials):
        del credentials
        return SimpleNamespace(
            job_id=JobId("job_starlink_suite_backfill"),
            request=SimpleNamespace(recording_id=recording_id),
        )

    stdout, stderr = StringIO(), StringIO()
    result = analysis_operator.main(
        [
            "submit-starlink-suite",
            "--recording-id",
            "rec_60s_backfill",
            "--science-manifest",
            str(DEPLOYMENT / "science.json"),
            "--credential-directory",
            str(tmp_path),
        ],
        stdout=stdout,
        stderr=stderr,
        starlink_suite_submitter=submitter,
        mode_lock_factory=lambda path: _ModeLock(lock_calls),
    )

    assert result == analysis_operator.ExitCode.OK
    assert json.loads(stdout.getvalue())["event"] == "gauss_starlink_suite_submitted"
    assert stderr.getvalue() == ""
    assert lock_calls == ["acquire", "release"]


def test_acquired_qam_backfill_command_is_narrow_and_bounded(tmp_path: Path) -> None:
    lock_calls: list[str] = []
    seen: list[RecordingId] = []

    def backfill(recording_id, credentials):
        del credentials
        seen.append(recording_id)
        return object()

    stdout, stderr = StringIO(), StringIO()
    result = analysis_operator.main(
        [
            "backfill-acquired-qam-v0-3",
            "--recording-id",
            "rec_existing_suite",
            "--science-manifest",
            str(DEPLOYMENT / "science.json"),
            "--credential-directory",
            str(tmp_path),
        ],
        stdout=stdout,
        stderr=stderr,
        acquired_qam_backfiller=backfill,
        mode_lock_factory=lambda path: _ModeLock(lock_calls),
    )

    assert result == analysis_operator.ExitCode.OK
    assert seen == [RecordingId("rec_existing_suite")]
    assert json.loads(stdout.getvalue())["event"] == (
        "gauss_acquired_qam_v0_3_backfill_complete"
    )
    assert stderr.getvalue() == ""
    assert lock_calls == ["acquire", "release"]


def test_submit_batch_uses_only_explicit_public_snapshot_and_checked_science(
    tmp_path: Path,
) -> None:
    snapshot = _terminal_batch()
    snapshot_path = tmp_path / "batch-snapshot.json"
    snapshot_path.write_bytes(encode_capture_batch_snapshot(snapshot))
    captured: list[object] = []
    lock_calls: list[str] = []

    def submit_batch(seen, selection, credentials):
        del credentials
        captured.extend((seen, selection))
        return _submitted_batch(seen)

    stdout, stderr = StringIO(), StringIO()
    result = analysis_operator.main(
        [
            "submit-batch",
            "--batch-snapshot",
            str(snapshot_path),
            "--science-manifest",
            str(DEPLOYMENT / "science.json"),
            "--credential-directory",
            str(tmp_path),
        ],
        stdout=stdout,
        stderr=stderr,
        batch_submitter=submit_batch,
        mode_lock_factory=lambda path: _ModeLock(lock_calls),
    )

    assert result == analysis_operator.ExitCode.OK
    assert captured[0] == snapshot
    selection = captured[1]
    assert selection.algorithm_ref == analysis_v1.RECORDING_ALGORITHM_REF
    assert selection.config_ref == analysis_v1.RECORDING_CONFIG_REF
    assert selection.dependency_refs == analysis_v1.RECORDING_DEPENDENCY_REFS
    payload = json.loads(stdout.getvalue())
    assert payload["event"] == "gauss_batch_submitted"
    assert payload["paired_analysis_eligibility"] == "eligible"
    assert payload["paired_science_submitted"] is False
    assert [item["recording_id"] for item in payload["recording_jobs"]] == [
        str(item.recording_id) for item in snapshot.successful_recordings
    ]
    assert stderr.getvalue() == ""
    assert lock_calls == ["acquire", "release"]


def test_batch_projection_failure_prevents_catalog_verification_and_enqueue() -> None:
    snapshot = _terminal_batch()
    calls: list[str] = []

    class Projection:
        def publish(self, view):
            del view
            calls.append("project")
            raise RuntimeError("projection unavailable")

    class Catalog:
        def get(self, recording_id):
            del recording_id
            calls.append("catalog")
            raise AssertionError("catalog must follow successful projection")

    class Jobs:
        def enqueue(self, *args, **kwargs):
            del args, kwargs
            calls.append("enqueue")

    with pytest.raises(RuntimeError, match="projection unavailable"):
        analysis_operator._submit_closed_batch_with_ports(
            snapshot,
            analysis_operator._batch_selection(),
            Catalog(),
            Jobs(),
            Projection(),
        )
    assert calls == ["project"]


def test_batch_projection_and_job_replay_converge_exactly() -> None:
    snapshot = _terminal_batch()
    publications = {item.recording_id: item for item in snapshot.successful_recordings}
    views: list[object] = []

    class Projection:
        def publish(self, view):
            views.append(view)
            return 1

    class Catalog:
        def get(self, recording_id):
            return publications.get(recording_id)

    jobs = InMemoryJobLeaseRepository()
    first = analysis_operator._submit_closed_batch_with_ports(
        snapshot,
        analysis_operator._batch_selection(),
        Catalog(),
        jobs,
        Projection(),
    )
    second = analysis_operator._submit_closed_batch_with_ports(
        snapshot,
        analysis_operator._batch_selection(),
        Catalog(),
        jobs,
        Projection(),
    )

    assert first.recording_jobs == second.recording_jobs
    assert len(views) == 2
    assert views[0] == views[1]
    assert views[0].batch_id == snapshot.batch_id


def test_drain_batch_reports_bounded_no_claimable_work_under_one_mode_lock(
    tmp_path: Path,
) -> None:
    snapshot = _terminal_batch()
    snapshot_path = tmp_path / "batch-snapshot.json"
    snapshot_path.write_bytes(encode_capture_batch_snapshot(snapshot))
    calls: list[str] = []
    analysis_results = iter((True, True, False))
    projection_results = iter((True, False))

    def submit_batch(seen, selection, credentials):
        del selection, credentials
        calls.append("submit")
        return _submitted_batch(seen)

    def process(config, credentials, output):
        del config, credentials, output
        calls.append("process")
        return next(analysis_results)

    def project(credentials):
        del credentials
        calls.append("project")
        return next(projection_results)

    stdout, stderr = StringIO(), StringIO()
    result = analysis_operator.main(
        [
            "drain-batch",
            "--batch-snapshot",
            str(snapshot_path),
            "--config",
            str(DEPLOYMENT / "analysis.json"),
            "--science-manifest",
            str(DEPLOYMENT / "science.json"),
            "--credential-directory",
            str(tmp_path),
            "--max-analysis-jobs",
            "4",
            "--max-projection-work",
            "3",
        ],
        stdout=stdout,
        stderr=stderr,
        batch_submitter=submit_batch,
        processor=process,
        projector=project,
        mode_lock_factory=lambda path: _ModeLock(calls),
    )

    assert result == analysis_operator.ExitCode.OK
    assert calls == [
        "acquire",
        "submit",
        "process",
        "process",
        "process",
        "project",
        "project",
        "release",
    ]
    payload = json.loads(stdout.getvalue())
    assert payload["analysis_processed"] == 2
    assert payload["analysis_no_claimable_work"] is True
    assert payload["feature_projections_processed"] == 1
    assert payload["feature_projection_no_claimable_work"] is True
    assert payload["paired_science_submitted"] is False
    assert stderr.getvalue() == ""


def test_submit_batch_lock_contention_precedes_credentials_and_database(
    tmp_path: Path,
) -> None:
    snapshot_path = tmp_path / "batch-snapshot.json"
    snapshot_path.write_bytes(encode_capture_batch_snapshot(_terminal_batch()))
    calls: list[str] = []

    def forbidden(*_args):
        raise AssertionError("credential or database dependency was called")

    stderr = StringIO()
    result = analysis_operator.main(
        [
            "submit-batch",
            "--batch-snapshot",
            str(snapshot_path),
            "--science-manifest",
            str(DEPLOYMENT / "science.json"),
            "--credential-directory",
            str(tmp_path),
        ],
        stdout=StringIO(),
        stderr=stderr,
        batch_submitter=forbidden,
        credential_factory=forbidden,
        mode_lock_factory=lambda path: _ModeLock(calls, fail=True),
    )

    assert result == analysis_operator.ExitCode.SUBMISSION_FAILED
    assert calls == ["acquire"]
    assert json.loads(stderr.getvalue()) == {"event": "gauss_batch_submission_failed"}


def test_submit_batch_releases_mode_lock_on_database_error(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "batch-snapshot.json"
    snapshot_path.write_bytes(encode_capture_batch_snapshot(_terminal_batch()))
    calls: list[str] = []

    def fail(*_args):
        calls.append("submit")
        raise RuntimeError("private database failure")

    result = analysis_operator.main(
        [
            "submit-batch",
            "--batch-snapshot",
            str(snapshot_path),
            "--science-manifest",
            str(DEPLOYMENT / "science.json"),
            "--credential-directory",
            str(tmp_path),
        ],
        stdout=StringIO(),
        stderr=StringIO(),
        batch_submitter=fail,
        mode_lock_factory=lambda path: _ModeLock(calls),
    )

    assert result == analysis_operator.ExitCode.SUBMISSION_FAILED
    assert calls == ["acquire", "submit", "release"]


def test_drain_lock_contention_precedes_database_and_cas_use(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "batch-snapshot.json"
    snapshot_path.write_bytes(encode_capture_batch_snapshot(_terminal_batch()))
    calls: list[str] = []

    def forbidden(*_args):
        raise AssertionError("database or CAS dependency was called")

    stderr = StringIO()
    result = analysis_operator.main(
        [
            "drain-batch",
            "--batch-snapshot",
            str(snapshot_path),
            "--config",
            str(DEPLOYMENT / "analysis.json"),
            "--science-manifest",
            str(DEPLOYMENT / "science.json"),
            "--credential-directory",
            str(tmp_path),
            "--max-analysis-jobs",
            "2",
            "--max-projection-work",
            "2",
        ],
        stdout=StringIO(),
        stderr=stderr,
        batch_submitter=forbidden,
        processor=forbidden,
        projector=forbidden,
        mode_lock_factory=lambda path: _ModeLock(calls, fail=True),
    )

    assert result == analysis_operator.ExitCode.ANALYSIS_FAILED
    assert calls == ["acquire"]
    assert json.loads(stderr.getvalue()) == {"event": "gauss_batch_drain_failed"}


def test_drain_releases_mode_lock_when_processing_fails(tmp_path: Path) -> None:
    snapshot = _terminal_batch()
    snapshot_path = tmp_path / "batch-snapshot.json"
    snapshot_path.write_bytes(encode_capture_batch_snapshot(snapshot))
    calls: list[str] = []

    def fail_process(config, credentials, output):
        del config, credentials, output
        calls.append("process")
        raise RuntimeError("private processing failure")

    result = analysis_operator.main(
        [
            "drain-batch",
            "--batch-snapshot",
            str(snapshot_path),
            "--config",
            str(DEPLOYMENT / "analysis.json"),
            "--science-manifest",
            str(DEPLOYMENT / "science.json"),
            "--credential-directory",
            str(tmp_path),
            "--max-analysis-jobs",
            "2",
            "--max-projection-work",
            "2",
        ],
        stdout=StringIO(),
        stderr=StringIO(),
        batch_submitter=lambda seen, selection, credentials: _submitted_batch(seen),
        processor=fail_process,
        projector=lambda credentials: pytest.fail("projection called"),
        mode_lock_factory=lambda path: _ModeLock(calls),
    )

    assert result == analysis_operator.ExitCode.ANALYSIS_FAILED
    assert calls == ["acquire", "process", "release"]


@pytest.mark.parametrize(
    ("command", "event"),
    [
        ("process-one", "gauss_analysis_cycle_complete"),
        ("project-one", "gauss_feature_projection_cycle_complete"),
        ("project-waterfall-one", "gauss_waterfall_projection_cycle_complete"),
        ("process-starlink-one", "gauss_starlink_analysis_cycle_complete"),
        ("project-starlink-one", "gauss_starlink_projection_cycle_complete"),
        (
            "process-starlink-suite-one",
            "gauss_starlink_suite_analysis_cycle_complete",
        ),
        (
            "project-starlink-suite-one",
            "gauss_starlink_suite_projection_cycle_complete",
        ),
    ],
)
def test_bounded_processing_commands_report_forward_progress(
    tmp_path: Path, command: str, event: str
) -> None:
    stdout, stderr = StringIO(), StringIO()
    lock_calls: list[str] = []
    arguments = [
        command,
        "--science-manifest",
        str(DEPLOYMENT / "science.json"),
        "--credential-directory",
        str(tmp_path),
    ]
    if command == "process-one":
        arguments.extend(["--config", str(DEPLOYMENT / "analysis.json")])

    result = analysis_operator.main(
        arguments,
        stdout=stdout,
        stderr=stderr,
        processor=lambda config, credentials, output: True,
        projector=lambda credentials: True,
        waterfall_projector=lambda credentials: True,
        starlink_processor=lambda credentials: True,
        starlink_projector=lambda credentials: True,
        starlink_suite_processor=lambda credentials: True,
        starlink_suite_projector=lambda credentials: True,
        mode_lock_factory=lambda path: (
            _ModeLock(lock_calls)
            if path == analysis_v1.MODE_LOCK_PATH
            else pytest.fail("wrong mode lock")
        ),
    )

    assert result == analysis_operator.ExitCode.OK
    expected = {
        "event": event,
        "forward_progress": True,
        "science_manifest_digest": str(analysis_v1.SCIENCE_MANIFEST_DIGEST),
    }
    if command in {"process-starlink-one", "project-starlink-one"}:
        expected["candidate_semantics"] = "uncalibrated-search-only"
    assert json.loads(stdout.getvalue()) == expected
    assert lock_calls == ["acquire", "release"]
    assert stderr.getvalue() == ""


def test_failures_are_sanitized(tmp_path: Path) -> None:
    stdout, stderr = StringIO(), StringIO()

    def fail(config, credentials):
        del config, credentials
        raise RuntimeError("postgresql://user:secret@host/catalog")

    result = analysis_operator.main(
        [
            "submit",
            "--recording-id",
            "rec_after_capture",
            "--science-manifest",
            str(DEPLOYMENT / "science.json"),
            "--credential-directory",
            str(tmp_path),
        ],
        stdout=stdout,
        stderr=stderr,
        submitter=fail,
        mode_lock_factory=lambda path: _ModeLock([]),
    )

    assert result == analysis_operator.ExitCode.SUBMISSION_FAILED
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == '{"event":"gauss_analysis_submission_failed"}\n'
    assert "secret" not in stderr.getvalue()
