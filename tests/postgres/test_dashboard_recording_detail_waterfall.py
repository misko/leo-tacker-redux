from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from playwright.sync_api import expect, sync_playwright
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.adapters.dashboard_postgres import PostgresDashboardRepository
from leo_flow.adapters.dashboard_projection_postgres import (
    PostgresAnalysisProjectionWriter,
    PostgresCaptureProjectionWriter,
)
from leo_flow.adapters.dashboard_recording_postgres import (
    PostgresRecordingWaterfallProjectionWriter,
    recording_waterfall_view_v0_1,
)
from leo_flow.application.projection_writers import (
    FeatureProjectionCommand,
    RecordingProjectionCommand,
)
from leo_flow.contracts.core import (
    V0_1,
    AnalysisRunId,
    Digest,
    Provenance,
    ReceiverChainId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.dashboard_waterfall import WaterfallProjectionState
from leo_flow.contracts.storage import ObjectRef, PublishedRecordingRef
from leo_flow.contracts.waterfall import (
    WaterfallBundleV0_1,
    WaterfallProductId,
    WaterfallProductRefV0_1,
    WaterfallTileV0_1,
    WaterfallTimeBinV0_1,
)
from leo_flow.dashboard.api import DashboardJsonApplicationV3
from leo_flow.dashboard.ui import DashboardUiApplication
from tests.projection_writer_fixtures import (
    feature_bundle_and_ref,
    published_recording,
    recording_manifest,
)


def _role_connect(postgres_dsn: str, role: str):
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        connection.execute(f"SET ROLE {role}")
        return connection

    return connect


def _catalog_recording(postgres_dsn: str, published: PublishedRecordingRef) -> None:
    ref = published.recording_object
    with psycopg.connect(postgres_dsn) as connection:
        for obj in (ref.data_object, ref.metadata_object):
            connection.execute(
                """
                INSERT INTO object_blob
                    (digest_algorithm, digest_value, byte_count, media_type,
                     format_id, locator)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    obj.digest.algorithm.value,
                    obj.digest.value,
                    obj.byte_count,
                    obj.media_type,
                    obj.format_id,
                    obj.locator,
                ),
            )
        connection.execute(
            """
            INSERT INTO recording
                (recording_id, data_digest_value, metadata_digest_value,
                 manifest_digest_value, idempotency_key, state)
            VALUES (%s, %s, %s, %s, %s, 'published')
            """,
            (
                str(ref.recording_id),
                ref.data_object.digest.value,
                ref.metadata_object.digest.value,
                ref.manifest_digest.value,
                f"detail:{ref.recording_id}",
            ),
        )


def _project_recording(postgres_dsn: str, index: int = 61):
    manifest = recording_manifest(index)
    published = published_recording(manifest)
    _catalog_recording(postgres_dsn, published)
    writer = PostgresCaptureProjectionWriter(_role_connect(postgres_dsn, "leo_capture"))
    receipt = writer.project_recording(
        RecordingProjectionCommand(manifest, published, True)
    )
    return manifest, published, writer, receipt


def _waterfall(published: PublishedRecordingRef):
    manifest = recording_manifest(61)
    segment = manifest.segments[0]
    start = segment.start_utc_ns
    sample_rate = segment.actual_sample_rate_hz
    window = 16
    midpoint = UtcNs(int(start) + round(window * 500_000_000 / sample_rate))
    tile = WaterfallTileV0_1(
        SegmentId(str(segment.segment_id)),
        ReceiverChainId(str(manifest.receiver_chain_ids[0])),
        start,
        segment.sample_count,
        segment.actual_center_frequency_hz,
        sample_rate,
        window,
        "counts-squared-per-bin",
        (-375_000.0, -125_000.0, 125_000.0, 375_000.0),
        (
            WaterfallTimeBinV0_1(
                0,
                window,
                midpoint,
                (-82.0, -65.0, -44.0, -76.0),
            ),
        ),
    )
    identity = published.recording_object.identity_digest()
    provenance = Provenance(
        "bounded-waterfall",
        "0.1.0",
        "fixture-commit",
        Digest.sha256(b"environment"),
        Digest.sha256(b"config"),
        (identity,),
        (Digest.sha256(b"algorithm"),),
        UtcNs(1),
        UtcNs(2),
        "fixture-host",
    )
    bundle = WaterfallBundleV0_1(
        SchemaRef(WaterfallBundleV0_1.SCHEMA_ID, V0_1),
        WaterfallProductId("waterfall_dashboard_pg"),
        AnalysisRunId("arun_dashboard_pg"),
        published.recording_object.recording_id,
        identity,
        provenance,
        (tile,),
    )
    bundle_ref = ObjectRef(
        Digest.sha256(b"durable-waterfall"),
        256,
        "application/json",
        "leo-waterfall-bundle-v0.1",
        "cas:sha256:" + Digest.sha256(b"durable-waterfall").value,
    )
    return bundle, WaterfallProductRefV0_1(
        bundle.product_id,
        bundle.analysis_run_id,
        bundle.recording_id,
        bundle_ref,
    )


@pytest.mark.integration
def test_capture_detail_is_atomic_idempotent_and_tracks_current_analysis_state(
    postgres_dsn: str,
) -> None:
    manifest, published, capture, first = _project_recording(postgres_dsn)
    assert (
        capture.project_recording(RecordingProjectionCommand(manifest, published, True))
        == first
    )
    dashboard = PostgresDashboardRepository(
        _role_connect(postgres_dsn, "leo_dashboard")
    )
    pending = dashboard.recording_capture_detail(manifest.recording_id)
    assert pending.plan_id == manifest.plan_id
    assert pending.segments[0].center_frequency_hz == 1_825_000_000.0
    assert pending.analysis_state == "pending"

    bundle, feature_ref = feature_bundle_and_ref(
        published.recording_object, manifest, 61
    )
    PostgresAnalysisProjectionWriter(
        _role_connect(postgres_dsn, "leo_analysis")
    ).project_features(FeatureProjectionCommand(bundle, feature_ref, published))
    complete = dashboard.recording_capture_detail(manifest.recording_id)
    assert complete.analysis_state == "complete"
    assert complete.segments == pending.segments

    corrected = capture.project_recording(
        RecordingProjectionCommand(manifest, published, False)
    )
    assert corrected.projection_sequences[-1] == first.projection_sequences[-1]
    assert not dashboard.recording_capture_detail(
        manifest.recording_id
    ).recording_object_available
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM dashboard_recording_detail_projection"
        ).fetchone() == (1,)


@pytest.mark.integration
def test_complete_waterfall_projection_is_idempotent_and_terminal(
    postgres_dsn: str,
) -> None:
    manifest, published, _capture, _receipt = _project_recording(postgres_dsn)
    bundle, ref = _waterfall(published)
    writer = PostgresRecordingWaterfallProjectionWriter(
        _role_connect(postgres_dsn, "leo_analysis")
    )
    first = writer.project_complete(bundle, ref)
    assert writer.project_complete(bundle, ref) == first
    expected = recording_waterfall_view_v0_1(bundle, ref)
    actual = PostgresDashboardRepository(
        _role_connect(postgres_dsn, "leo_dashboard")
    ).recording_waterfall(manifest.recording_id)
    assert actual == expected
    assert actual.tiles[0].power_reference == "counts-squared-per-bin"

    pending = replace(
        actual,
        state=WaterfallProjectionState.PENDING,
        reason_code=None,
        tiles=(),
    )
    with pytest.raises(psycopg.errors.UniqueViolation, match="regression"):
        writer.publish(pending)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM dashboard_recording_waterfall_projection"
        ).fetchone() == (1,)


@pytest.mark.integration
def test_projection_roles_are_directional_and_raw_malformed_payload_is_rejected(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        privileges = connection.execute(
            """
            SELECT has_table_privilege(
                       'leo_dashboard', 'dashboard_recording_detail_projection',
                       'SELECT'),
                   has_table_privilege(
                       'leo_dashboard', 'dashboard_recording_waterfall_projection',
                       'SELECT'),
                   has_table_privilege(
                       'leo_dashboard', 'dashboard_recording_detail_projection',
                       'INSERT'),
                   has_table_privilege(
                       'leo_capture', 'dashboard_recording_detail_projection',
                       'SELECT'),
                   has_table_privilege(
                       'leo_analysis', 'dashboard_recording_waterfall_projection',
                       'SELECT'),
                   has_function_privilege(
                       'leo_capture', 'publish_dashboard_recording_detail(jsonb)',
                       'EXECUTE'),
                   has_function_privilege(
                       'leo_analysis', 'publish_dashboard_recording_waterfall(jsonb)',
                       'EXECUTE'),
                   has_function_privilege(
                       'leo_dashboard', 'publish_dashboard_recording_waterfall(jsonb)',
                       'EXECUTE')
            """
        ).fetchone()
    assert privileges == (True, True, False, False, False, True, True, False)

    with (
        _role_connect(postgres_dsn, "leo_analysis")() as connection,
        pytest.raises(psycopg.errors.InvalidParameterValue),
    ):
        connection.execute(
            "SELECT publish_dashboard_recording_waterfall(%s::jsonb)",
            (Jsonb({"recording_id": "rec_smuggled", "tiles": []}),),
        )


@contextmanager
def _running_dashboard(postgres_dsn: str):
    queries = PostgresDashboardRepository(_role_connect(postgres_dsn, "leo_dashboard"))
    application = DashboardUiApplication(
        DashboardJsonApplicationV3(queries, queries, queries, queries, queries)
    )
    server = StdlibDashboardServer(request_timeout_s=0.01)
    server.preflight("127.0.0.1", 0)
    stopped = threading.Event()

    def serve() -> None:
        while not stopped.is_set():
            server.serve_once(application)

    worker = threading.Thread(target=serve, name="pg-recording-dashboard")
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.bound_port}"
    finally:
        stopped.set()
        worker.join(timeout=1)
        server.close(1)
        assert not worker.is_alive()


def _browser_environment() -> dict[str, str | float | bool]:
    environment: dict[str, str | float | bool] = dict(os.environ)
    root = Path.home() / ".cache" / "ms-playwright" / "ubuntu-libs"
    if root.is_dir():
        environment["LD_LIBRARY_PATH"] = str(root / "usr/lib/x86_64-linux-gnu")
        environment["FONTCONFIG_FILE"] = str(root / "etc/fonts/fonts.conf")
        environment["FONTCONFIG_SYSROOT"] = str(root)
    return environment


@pytest.mark.integration
def test_real_postgres_projection_renders_in_real_browser(postgres_dsn: str) -> None:
    manifest, published, _capture, _receipt = _project_recording(postgres_dsn)
    bundle, ref = _waterfall(published)
    PostgresRecordingWaterfallProjectionWriter(
        _role_connect(postgres_dsn, "leo_analysis")
    ).project_complete(bundle, ref)

    with _running_dashboard(postgres_dsn) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=_browser_environment())
        try:
            page = browser.new_page()
            response = page.goto(f"{base_url}/recordings/{manifest.recording_id}")
            assert response is not None and response.ok
            expect(page.locator("#capture-page-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#segments-body")).to_contain_text(
                str(manifest.segments[0].segment_id)
            )
            expect(page.locator("#waterfall-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#waterfall-canvas")).to_have_attribute(
                "aria-label",
                "Waterfall for seg_projection_61, receiver rx_projection; "
                "1 time bins by 4 frequency bins",
            )
        finally:
            browser.close()
