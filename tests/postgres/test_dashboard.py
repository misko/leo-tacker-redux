from __future__ import annotations

import hashlib
import socket

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.adapters.dashboard_postgres import PostgresDashboardRepository
from leo_flow.contracts.capture import ActivityKind
from leo_flow.contracts.core import RadioId, RecordingId, UtcNs
from leo_flow.contracts.dashboard import TimeRangeQuery
from leo_flow.dashboard.repository import DashboardNotFound, InvalidCursor
from leo_flow.deployments import dashboard_v1
from leo_flow.services import (
    AdapterBuildContext,
    Capability,
    DashboardServiceConfig,
    Process,
    RuntimeConfig,
)
from leo_flow.services.lifecycle import NullDiagnosticSink
from leo_flow.storage.postgres_catalog import connection_factory


def _unused_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _dashboard_connection(postgres_dsn: str) -> psycopg.Connection[dict[str, object]]:
    connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
    connection.execute("SET ROLE leo_dashboard")
    return connection


def _catalog_recording(
    connection: psycopg.Connection[tuple[object, ...]], value: str
) -> None:
    for suffix in ("data", "meta"):
        digest = hashlib.sha256(f"{value}:{suffix}".encode()).hexdigest()
        connection.execute(
            """
            INSERT INTO object_blob
                (digest_algorithm, digest_value, byte_count, media_type,
                 format_id, locator)
            VALUES ('sha256', %s, 1, 'test/data', 'test-v1', %s)
            """,
            (digest, f"object://{value}/{suffix}"),
        )
    connection.execute(
        """
        INSERT INTO recording
            (recording_id, data_digest_value, metadata_digest_value,
             manifest_digest_value, idempotency_key, state)
        VALUES (%s, %s, %s, %s, %s, 'published')
        """,
        (
            value,
            hashlib.sha256(f"{value}:data".encode()).hexdigest(),
            hashlib.sha256(f"{value}:meta".encode()).hexdigest(),
            hashlib.sha256(f"{value}:manifest".encode()).hexdigest(),
            f"key-{value}",
        ),
    )


def _recording(
    connection: psycopg.Connection[tuple[object, ...]],
    value: str,
    radio: str,
    started: int,
) -> None:
    _catalog_recording(connection, value)
    connection.execute(
        """
        INSERT INTO dashboard_recording_projection
            (recording_id, radio_id, started_utc_ns, finished_utc_ns,
             analysis_state, segment_count, recording_object_available)
        VALUES (%s, %s, %s, %s, 'complete', 2, true)
        """,
        (value, radio, started, started + 10),
    )


@pytest.mark.integration
def test_activity_counts_are_exact_by_radio_kind_and_half_open_interval(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        _recording(connection, "rec_1", "radio_a", 100)
        _recording(connection, "rec_2", "radio_b", 100)
        statement = """
            INSERT INTO dashboard_activity_projection
                (activity_id, recording_id, radio_id, kind, started_utc_ns)
            VALUES (%s, %s, %s, %s, %s)
            """
        for parameters in [
            ("act_1", "rec_1", "radio_a", "scan", 100),
            ("act_2", "rec_1", "radio_a", "scan", 150),
            ("act_3", "rec_1", "radio_a", "dwell", 199),
            ("act_4", "rec_1", "radio_a", "dwell", 200),
            ("act_5", "rec_2", "radio_b", "scan", 175),
            ("act_6", "rec_2", "radio_b", "calibration", 176),
        ]:
            connection.execute(statement, parameters)
        # Reprojecting a logical activity must not double-count it.
        connection.execute(statement, ("act_1", "rec_1", "radio_a", "scan", 100))
    result = PostgresDashboardRepository(connection_factory(postgres_dsn)).activity(
        TimeRangeQuery(UtcNs(100), UtcNs(200))
    )
    assert [(str(row.radio_id), row.kind, row.count) for row in result.counts] == [
        ("radio_a", ActivityKind.DWELL, 1),
        ("radio_a", ActivityKind.SCAN, 2),
        ("radio_b", ActivityKind.CALIBRATION, 1),
        ("radio_b", ActivityKind.SCAN, 1),
    ]
    filtered = PostgresDashboardRepository(connection_factory(postgres_dsn)).activity(
        TimeRangeQuery(UtcNs(100), UtcNs(200), (RadioId("radio_a"),))
    )
    assert {str(row.radio_id) for row in filtered.counts} == {"radio_a"}


@pytest.mark.integration
def test_recording_pagination_is_keyset_and_snapshot_stable(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        _recording(connection, "rec_1", "radio_a", 100)
        _recording(connection, "rec_2", "radio_a", 200)
        _recording(connection, "rec_3", "radio_a", 300)
    repository = PostgresDashboardRepository(
        connection_factory(postgres_dsn), page_size=2
    )
    query = TimeRangeQuery(UtcNs(0), UtcNs(1_000))
    first = repository.recent_recordings(query)
    assert [str(row.recording_id) for row in first.items] == ["rec_3", "rec_2"]
    assert first.next_cursor is not None
    with psycopg.connect(postgres_dsn) as connection:
        _recording(connection, "rec_4", "radio_a", 400)
    second = repository.recent_recordings(query, first.next_cursor)
    assert [str(row.recording_id) for row in second.items] == ["rec_1"]
    assert second.next_cursor is None


@pytest.mark.integration
def test_details_features_models_tracks_health_and_missing_rows(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        _recording(connection, "rec_1", "radio_a", 100)
        connection.execute(
            """
            INSERT INTO dashboard_activity_projection
                (activity_id, recording_id, radio_id, kind, started_utc_ns)
            VALUES ('act_1', 'rec_1', 'radio_a', 'scan', 100)
            """
        )
        statement = """
            INSERT INTO dashboard_feature_projection
                (feature_id, recording_id, method_id, score, score_semantics)
            VALUES (%s, 'rec_1', %s, %s, 'probability')
            """
        for parameters in [("feature_1", "fft", 0.5), ("feature_2", "periodic", 0.75)]:
            connection.execute(statement, parameters)
        connection.execute(
            """
            INSERT INTO dashboard_model_projection
                (model_snapshot_id, release_alias, parameter_count, warnings)
            VALUES ('model_1', 'production', 3, '["limited-data"]')
            """
        )
        connection.execute(
            """
            INSERT INTO dashboard_track_projection
                (track_id, model_snapshot_id, radio_id,
                 started_utc_ns, finished_utc_ns)
            VALUES ('track_1', 'model_1', 'radio_a', 100, 110)
            """
        )
        connection.execute(
            """
            INSERT INTO dashboard_storage_health_projection
                (available, total_bytes, free_bytes)
            VALUES (true, 1000, 250)
            """
        )
    repository = PostgresDashboardRepository(connection_factory(postgres_dsn))
    detail = repository.recording_detail(RecordingId("rec_1"))
    assert detail.segment_count == 2
    assert detail.summary.activity_kinds == (ActivityKind.SCAN,)
    assert [
        row.feature_id
        for row in repository.recording_features(RecordingId("rec_1"), "*").items
    ] == ["feature_1", "feature_2"]
    assert [
        row.feature_id
        for row in repository.recording_features(RecordingId("rec_1"), "fft").items
    ] == ["feature_1"]
    assert repository.model_snapshot("production").warnings == ("limited-data",)
    tracks = repository.tracks(TimeRangeQuery(UtcNs(100), UtcNs(200)))
    assert [row.track_id for row in tracks.items] == ["track_1"]
    assert repository.storage_health().free_bytes == 250
    with pytest.raises(DashboardNotFound):
        repository.recording_detail(RecordingId("rec_missing"))


@pytest.mark.integration
def test_inputs_are_parameterized_and_cursors_are_query_bound(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        _recording(connection, "rec_1", "radio_a", 100)
        statement = """
            INSERT INTO dashboard_feature_projection
                (feature_id, recording_id, method_id, score, score_semantics)
            VALUES (%s, 'rec_1', 'fft', 0.5, 'probability')
            """
        for parameters in [("feature_1",), ("feature_2",)]:
            connection.execute(statement, parameters)
        connection.execute(
            """
            INSERT INTO dashboard_model_projection
                (model_snapshot_id, release_alias, parameter_count)
            VALUES ('model_1', 'production', 3)
            """
        )
    repository = PostgresDashboardRepository(
        connection_factory(postgres_dsn), page_size=1
    )
    with pytest.raises(DashboardNotFound):
        repository.model_snapshot("production' OR true --")
    page = repository.recording_features(RecordingId("rec_1"), "*")
    assert page.next_cursor is not None
    with pytest.raises(InvalidCursor):
        repository.recording_features(RecordingId("rec_1"), "fft", page.next_cursor)
    with pytest.raises(ValueError, match="between"):
        PostgresDashboardRepository(connection_factory(postgres_dsn), page_size=201)


@pytest.mark.integration
def test_empty_projections_have_explicit_empty_or_missing_semantics(
    postgres_dsn: str,
) -> None:
    repository = PostgresDashboardRepository(connection_factory(postgres_dsn))
    query = TimeRangeQuery(UtcNs(0), UtcNs(1))
    assert repository.recent_recordings(query).items == ()
    assert repository.activity(query).counts == ()
    assert repository.tracks(query).items == ()
    assert not repository.storage_health().available
    with pytest.raises(DashboardNotFound):
        repository.recording_features(RecordingId("rec_missing"), "*")


@pytest.mark.integration
def test_repository_runs_with_dashboard_role_and_read_only_transaction(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        _recording(connection, "rec_1", "radio_a", 100)

    repository = PostgresDashboardRepository(
        lambda: _dashboard_connection(postgres_dsn), page_size=1
    )
    page = repository.recent_recordings(TimeRangeQuery(UtcNs(0), UtcNs(200)))
    assert [str(row.recording_id) for row in page.items] == ["rec_1"]


@pytest.mark.integration
def test_dashboard_v1_becomes_ready_only_after_real_read_only_query_preflight(
    postgres_dsn: str,
) -> None:
    context = AdapterBuildContext(
        Process.DASHBOARD,
        Capability.QUERY_PROJECTION,
        dashboard_v1.QUERY_PROJECTION_REF,
        {dashboard_v1.DATABASE_SECRET: postgres_dsn},
    )
    queries = dashboard_v1._postgres_query_projection(context)
    server = StdlibDashboardServer(request_timeout_s=0.01)
    config = DashboardServiceConfig(
        1,
        "dashboard",
        RuntimeConfig("dashboard-pg-test", 0.01, 0.1, ()),
        dashboard_v1.QUERY_PROJECTION_REF,
        dashboard_v1.SERVER_REF,
        "127.0.0.1",
        _unused_loopback_port(),
    )
    service = dashboard_v1._build_dashboard(
        config,
        {
            Capability.QUERY_PROJECTION: queries,
            Capability.DASHBOARD_SERVER: server,
        },
        NullDiagnosticSink(),
    )
    assert not service.health().ready
    assert not service.run_once()
    assert service.health().ready
    service.shutdown()
