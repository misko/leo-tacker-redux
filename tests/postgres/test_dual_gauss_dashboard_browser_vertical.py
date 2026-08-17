from __future__ import annotations

import os
import struct
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import psycopg
import pytest
from playwright.sync_api import Page, expect, sync_playwright
from psycopg.rows import dict_row

from leo_flow.adapters.dashboard_batch_postgres import (
    PostgresBatchAwareAnalysisProjectionWriter,
    PostgresCaptureBatchDashboardRepository,
    PostgresCaptureBatchProjectionWriter,
)
from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.adapters.dashboard_postgres import PostgresDashboardRepository
from leo_flow.adapters.dashboard_projection_postgres import (
    PostgresAnalysisProjectionWriter,
    PostgresCaptureProjectionWriter,
)
from leo_flow.adapters.feature_postgres_catalog import PostgresFeatureSetCatalog
from leo_flow.adapters.feature_projection_work_postgres import (
    PostgresFeatureProjectionWorkRepository,
)
from leo_flow.analysis.recording import DurableFeatureSetRepository
from leo_flow.application.capture_batch_dashboard import (
    initial_capture_batch_dashboard_view,
)
from leo_flow.application.feature_projection_work import FeatureProjectionWorker
from leo_flow.application.projection_writers import (
    AnalysisProjectionWriter,
    FeatureProjectionCommand,
    RecordingProjectionCommand,
)
from leo_flow.contracts.capture import (
    ActivityKind,
    ActivityManifest,
    ActivityRequest,
    CapturePlan,
    CompletedLocalRecording,
    GainMode,
    GainSetting,
    RecordingManifest,
    SegmentManifest,
    SegmentRequest,
)
from leo_flow.contracts.capture_batch import (
    CaptureAttemptOutcome,
    CaptureAttemptState,
    CaptureBatchDefinition,
    CaptureBatchMode,
    CaptureBatchSnapshot,
    ExpectedCaptureAttempt,
)
from leo_flow.contracts.continuity import (
    CaptureProvenance,
    ContinuityStatus,
    RefillMetadata,
    SegmentContinuity,
)
from leo_flow.contracts.core import (
    ActivityId,
    CaptureAttemptId,
    CaptureBatchId,
    HardwareSnapshotId,
    PlanId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    StationId,
    UtcNs,
)
from leo_flow.contracts.dashboard_batch import DashboardAnalysisState
from leo_flow.contracts.features import FeatureSetBundle
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.dashboard.api import DashboardJsonApplicationV2
from leo_flow.dashboard.ui import DashboardUiApplication
from leo_flow.deployments.offline_analysis_v1 import (
    DATABASE_SECRET,
    FEATURE_PUBLISHER_REF,
    JOB_REPOSITORY_REF,
    MODEL_PUBLISHER_REF,
    RECORDING_READER_REF,
    build_station_plugin,
)
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from leo_flow.services.bootstrap import assemble_service
from leo_flow.services.capture_batch_analysis import (
    ClosedBatchAnalysisSelection,
    ClosedBatchAnalysisSubmissionService,
)
from leo_flow.services.config import AnalysisServiceConfig, RuntimeConfig
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.local_recording import RootedSigMFRecordingStore
from leo_flow.storage.postgres_catalog import (
    PostgresRecordingCatalog,
    PostgresRecordingPublisher,
)
from leo_flow.storage.recording_codec import SigMFRecordingWriter
from leo_station.analysis_v1 import (
    RECORDING_ALGORITHM_REF,
    RECORDING_CONFIG_REF,
    RECORDING_DEPENDENCY_REFS,
    SCIENTIFIC,
)

_BASE_UTC_NS = 1_700_000_000_000_000_000
_SAMPLE_COUNT = 256


def _connect_as(postgres_dsn: str, role: str):
    connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
    connection.execute(f"SET ROLE {role}")
    return connection


def _connect(postgres_dsn: str, role: str):
    return lambda: _connect_as(postgres_dsn, role)


@dataclass(frozen=True)
class _PublishedFixture:
    plan: CapturePlan
    manifest: RecordingManifest
    local: CompletedLocalRecording
    published: PublishedRecordingRef
    publisher: PostgresRecordingPublisher


def _publish_recording(
    postgres_dsn: str,
    root: Path,
    blobs: FileSystemBlobStore,
    index: int,
) -> _PublishedFixture:
    suffix = chr(ord("a") + index)
    receiver_ids = (
        ReceiverChainId(f"rx_vertical_{suffix}_1"),
        ReceiverChainId(f"rx_vertical_{suffix}_2"),
    )
    segment_request = SegmentRequest(
        SegmentId(f"seg_vertical_{suffix}"),
        11_325_000_000.0 + index * 2_500_000.0,
        2_500_000.0,
        2_500_000.0,
        receiver_ids,
        GainSetting(GainMode.MANUAL, 50.0),
        sample_count=_SAMPLE_COUNT,
    )
    activity_id = ActivityId(f"act_vertical_{suffix}")
    plan = CapturePlan(
        SchemaRef(CapturePlan.SCHEMA_ID),
        PlanId(f"plan_vertical_{suffix}"),
        RadioId(f"radio_vertical_{suffix}"),
        receiver_ids,
        (ActivityRequest(activity_id, ActivityKind.DWELL, (segment_request,)),),
    )
    started = _BASE_UTC_NS + index * 1_000_000_000
    segment = SegmentManifest(
        segment_request.segment_id,
        segment_request,
        segment_request.center_frequency_hz,
        segment_request.sample_rate_hz,
        segment_request.bandwidth_hz,
        segment_request.gain,
        UtcNs(started),
        100,
        _SAMPLE_COUNT,
        (_SAMPLE_COUNT, 2, 2),
    )
    manifest = RecordingManifest(
        SchemaRef(RecordingManifest.SCHEMA_ID),
        RecordingId(f"rec_vertical_{suffix}"),
        UtcNs(started - 1_000_000),
        UtcNs(started),
        UtcNs(started + 10_000_000),
        StationId("station_vertical"),
        plan.radio_id,
        f"offline-fake-v5-{suffix}",
        receiver_ids,
        "test-clock-locked",
        HardwareSnapshotId(f"hw_vertical_{suffix}"),
        (
            ActivityManifest(
                activity_id,
                ActivityKind.DWELL,
                UtcNs(started),
                UtcNs(started + 10_000_000),
                (segment.segment_id,),
            ),
        ),
        (segment,),
        plan.plan_id,
        "synthetic-v5-public-api",
    )
    recording_root = root / "capture-spool" / suffix
    destination = recording_root / str(manifest.recording_id)
    session = SigMFRecordingWriter().begin(
        manifest.recording_id,
        plan,
        manifest.hardware_metadata_snapshot_id,
        str(destination),
    )
    values = tuple(
        ((sample * (index + 1) + component * 13) % 801) - 400
        for sample in range(_SAMPLE_COUNT)
        for component in range(4)
    )
    iq = struct.pack(f"<{len(values)}h", *values)
    refill = RefillMetadata(
        0,
        0,
        _SAMPLE_COUNT,
        index + 1,
        10 + index,
        100_000 + index * _SAMPLE_COUNT,
        1_000_000 + index * 1_000_000,
        1_100_000 + index * 1_000_000,
        started,
        started + 102_400,
        100,
        (40.0, 41.0),
        (40.0, 41.0),
        (-50.0, -51.0),
        (-50.0, -51.0),
    )
    session.append_refill(segment.segment_id, iq, refill)
    session.record_continuity(
        segment.segment_id,
        SegmentContinuity(
            ContinuityStatus.VERIFIED_CONTIGUOUS,
            receiver_ids,
            CaptureProvenance(
                "v5.0-test-fixture",
                "offline-no-radio",
                "0.25",
                "v3",
                "metadata=1",
            ),
            (refill,),
        ),
    )
    session.finish_segment(segment)
    local = session.finalize(manifest)
    publisher = PostgresRecordingPublisher(
        RootedSigMFRecordingStore(recording_root),
        blobs,
        PostgresRecordingCatalog(_connect(postgres_dsn, "leo_capture")),
    )
    published = publisher.publish(local, idempotency_key=f"vertical:{suffix}")
    PostgresCaptureProjectionWriter(
        _connect(postgres_dsn, "leo_capture")
    ).project_recording(RecordingProjectionCommand(manifest, published, True))
    return _PublishedFixture(plan, manifest, local, published, publisher)


def _terminal_batch(
    fixtures: tuple[_PublishedFixture, _PublishedFixture],
) -> CaptureBatchSnapshot:
    expected = tuple(
        ExpectedCaptureAttempt(
            CaptureAttemptId(f"cattempt_vertical_{suffix}"),
            fixture.plan.radio_id,
            fixture.plan.plan_id,
            fixture.manifest.capture_started_utc_ns,
        )
        for suffix, fixture in zip(("a", "b"), fixtures, strict=True)
    )
    definition = CaptureBatchDefinition(
        SchemaRef(CaptureBatchDefinition.SCHEMA_ID),
        CaptureBatchId("cbatch_vertical_independent"),
        CaptureBatchMode.INDEPENDENT,
        cast(tuple[ExpectedCaptureAttempt, ExpectedCaptureAttempt], expected),
    )
    outcomes = tuple(
        CaptureAttemptOutcome(
            SchemaRef(CaptureAttemptOutcome.SCHEMA_ID),
            definition.batch_id,
            attempt.attempt_id,
            attempt.radio_id,
            attempt.plan_id,
            CaptureAttemptState.SUCCEEDED,
            fixture.manifest.capture_finished_utc_ns,
            fixture.manifest.capture_started_utc_ns,
            fixture.published,
        )
        for attempt, fixture in zip(expected, fixtures, strict=True)
    )
    return CaptureBatchSnapshot(
        SchemaRef(CaptureBatchSnapshot.SCHEMA_ID),
        definition,
        cast(tuple[CaptureAttemptOutcome, CaptureAttemptOutcome], outcomes),
        2,
    )


def _analysis_config() -> AnalysisServiceConfig:
    return AnalysisServiceConfig(
        1,
        "analysis",
        RuntimeConfig("dual-gauss-vertical", 0.01, 1.0, (DATABASE_SECRET,)),
        JOB_REPOSITORY_REF,
        RECORDING_READER_REF,
        FEATURE_PUBLISHER_REF,
        MODEL_PUBLISHER_REF,
    )


class _Diagnostics:
    def emit(self, event: object) -> None:
        del event


class _RecordingFeatureProjection:
    def __init__(self, delegate: PostgresBatchAwareAnalysisProjectionWriter) -> None:
        self.delegate = delegate
        self.commands: list[FeatureProjectionCommand] = []

    def project_features(self, command: FeatureProjectionCommand):
        self.commands.append(command)
        return self.delegate.project_features(command)


def _projection_worker(
    postgres_dsn: str,
    cas_root: Path,
    writer: _RecordingFeatureProjection,
) -> FeatureProjectionWorker:
    connect = _connect(postgres_dsn, "leo_analysis")
    return FeatureProjectionWorker(
        PostgresFeatureProjectionWorkRepository(connect),
        DurableFeatureSetRepository(
            FileSystemBlobStore(cas_root), PostgresFeatureSetCatalog(connect)
        ),
        PostgresRecordingCatalog(connect),
        cast(AnalysisProjectionWriter, writer),
        worker_id="dual-gauss-projection",
        lease_ttl_s=30.0,
        retry_delay_s=0.01,
    )


@contextmanager
def _running_dashboard(postgres_dsn: str) -> Iterator[str]:
    server = StdlibDashboardServer(request_timeout_s=0.01)
    server.preflight("127.0.0.1", 0)
    queries = PostgresDashboardRepository(
        _connect(postgres_dsn, "leo_dashboard"), page_size=20
    )
    application = DashboardUiApplication(DashboardJsonApplicationV2(queries, queries))
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            server.serve_once(application)

    worker = threading.Thread(target=serve, name="dual-gauss-dashboard")
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.bound_port}"
    finally:
        stop.set()
        worker.join(timeout=1)
        server.close(1)
        assert not worker.is_alive()


def _freeze_browser_clock(page: Page) -> None:
    page.add_init_script(
        f"""
        (() => {{
          const fixedNow = {(_BASE_UTC_NS // 1_000_000) + 10_000};
          const NativeDate = Date;
          class FixedDate extends NativeDate {{
            constructor(...args) {{ super(...(args.length ? args : [fixedNow])); }}
            static now() {{ return fixedNow; }}
          }}
          FixedDate.parse = NativeDate.parse;
          FixedDate.UTC = NativeDate.UTC;
          globalThis.Date = FixedDate;
        }})();
        """
    )


def _browser_environment() -> dict[str, str | float | bool]:
    environment: dict[str, str | float | bool] = dict(os.environ)
    root = Path.home() / ".cache" / "ms-playwright" / "ubuntu-libs"
    environment["LD_LIBRARY_PATH"] = str(root / "usr/lib/x86_64-linux-gnu")
    environment["FONTCONFIG_FILE"] = str(root / "etc/fonts/fonts.conf")
    environment["FONTCONFIG_SYSROOT"] = str(root)
    return environment


def _counts(postgres_dsn: str) -> tuple[int, ...]:
    with psycopg.connect(postgres_dsn) as connection:
        counts = []
        for table in (
            "recording",
            "feature_set",
            "job",
            "feature_projection_work",
            "dashboard_recording_projection",
            "dashboard_feature_projection",
            "dashboard_capture_batch_projection",
            "dashboard_capture_attempt_projection",
        ):
            row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
            assert row is not None
            counts.append(int(row[0]))
        return tuple(counts)


@pytest.mark.integration
def test_dual_capture_exact_gauss_analysis_projection_and_browser_replay(
    postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cas_root = tmp_path / "cas"
    blobs = FileSystemBlobStore(cas_root)
    fixtures = (
        _publish_recording(postgres_dsn, tmp_path, blobs, 0),
        _publish_recording(postgres_dsn, tmp_path, blobs, 1),
    )
    snapshot = _terminal_batch(fixtures)
    initial_view = initial_capture_batch_dashboard_view(snapshot)
    batch_writer = PostgresCaptureBatchProjectionWriter(
        _connect(postgres_dsn, "leo_capture")
    )
    initial_batch_sequence = batch_writer.publish(initial_view)

    analysis_connect = _connect(postgres_dsn, "leo_analysis")
    jobs = PostgresJobLeaseRepository(analysis_connect)
    submission = ClosedBatchAnalysisSubmissionService(
        PostgresRecordingCatalog(analysis_connect), jobs
    )
    selection = ClosedBatchAnalysisSelection(
        RECORDING_ALGORITHM_REF,
        RECORDING_CONFIG_REF,
        RECORDING_DEPENDENCY_REFS,
        SchemaRef(FeatureSetBundle.SCHEMA_ID),
    )
    submitted = submission.submit(snapshot, selection)
    assert len(submitted.recording_jobs) == 2

    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "catalog-dsn").write_text(postgres_dsn, encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    service = assemble_service(
        _analysis_config(),
        build_station_plugin(SCIENTIFIC, cas_root=cas_root),
        diagnostics=_Diagnostics(),
    )
    try:
        assert service.run_once()
        assert service.run_once()
        assert not service.run_once()
    finally:
        service.shutdown()

    batch_aware = PostgresBatchAwareAnalysisProjectionWriter(
        analysis_connect, PostgresAnalysisProjectionWriter(analysis_connect)
    )
    recording_writer = _RecordingFeatureProjection(batch_aware)
    projection = _projection_worker(postgres_dsn, cas_root, recording_writer)
    assert projection.process_one_work()
    assert projection.process_one_work()
    assert not projection.process_one_work()
    assert len(recording_writer.commands) == 2

    dashboard_batches = PostgresCaptureBatchDashboardRepository(
        _connect(postgres_dsn, "leo_dashboard")
    )
    completed_view = dashboard_batches.capture_batch(snapshot.batch_id)
    assert all(
        attempt.analysis_state is DashboardAnalysisState.COMPLETE
        and attempt.analysis_result_available
        for attempt in completed_view.attempts
    )
    before_counts = _counts(postgres_dsn)
    before_cas = tuple(
        sorted(
            path.relative_to(cas_root) for path in cas_root.rglob("*") if path.is_file()
        )
    )

    replay_batch_sequence = batch_writer.publish(initial_view)
    assert replay_batch_sequence > initial_batch_sequence
    assert batch_writer.publish(initial_view) == replay_batch_sequence
    assert submission.submit(snapshot, selection) == submitted
    replay_service = assemble_service(
        _analysis_config(),
        build_station_plugin(SCIENTIFIC, cas_root=cas_root),
        diagnostics=_Diagnostics(),
    )
    try:
        assert not replay_service.run_once()
    finally:
        replay_service.shutdown()
    assert not projection.process_one_work()
    for command in recording_writer.commands:
        batch_aware.project_features(command)
    for fixture in fixtures:
        assert (
            fixture.publisher.publish(
                fixture.local,
                idempotency_key=f"vertical:{str(fixture.plan.radio_id)[-1]}",
            )
            == fixture.published
        )
    assert _counts(postgres_dsn) == before_counts
    assert (
        tuple(
            sorted(
                path.relative_to(cas_root)
                for path in cas_root.rglob("*")
                if path.is_file()
            )
        )
        == before_cas
    )
    assert dashboard_batches.capture_batch(snapshot.batch_id) == completed_view

    with _running_dashboard(postgres_dsn) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=_browser_environment())
        try:
            page = browser.new_page()
            _freeze_browser_clock(page)
            response = page.goto(base_url)
            assert response is not None and response.ok
            expect(page.locator("#app-status")).to_have_attribute("data-state", "ready")
            batch = page.locator(f'[data-batch-id="{snapshot.batch_id}"]')
            expect(batch).to_have_count(2)
            expect(batch.locator(".capture-status-icon")).to_have_count(2)
            expect(
                batch.locator('.capture-status-icon[data-state="succeeded"]')
            ).to_have_count(2)
            expect(batch.locator(".analysis-status-icon")).to_have_count(2)
            expect(
                batch.locator('.analysis-status-icon[data-state="complete"]')
            ).to_have_count(2)
            expect(batch.locator(".pilot-detection-counts")).to_have_text(
                ["— / —", "— / —"]
            )

            expect(page.locator("#recordings-table tbody tr")).to_have_count(2)
            for fixture in fixtures:
                recording_id = str(fixture.manifest.recording_id)
                page.get_by_role("button", name=recording_id, exact=True).click()
                expect(page.locator("#recording-detail")).to_have_attribute(
                    "data-state", "ready"
                )
                expect(page.locator("#features-state")).to_have_attribute(
                    "data-state", "ready"
                )
                expect(page.locator("#features-list li")).to_have_count(4)
                expect(page.locator("#features-list")).to_contain_text(
                    "rms-magnitude-counts"
                )
                expect(page.locator("#features-list")).to_contain_text(
                    "peak-psd-to-median-psd-ratio"
                )
        finally:
            browser.close()
