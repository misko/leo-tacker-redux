from __future__ import annotations

import json
from pathlib import Path

from leo_flow.adapters.dashboard_postgres import PostgresDashboardRepository
from leo_flow.adapters.starlink_symbolwise_replay_postgres import (
    PostgresStarlinkSymbolwiseReplayRepositoryV0_1,
)
from leo_flow.analysis.recording.starlink_symbolwise_replay_product_persistence import (
    DurableRecordingStarlinkSymbolwiseReplayQueryV0_1,
)
from leo_flow.dashboard.api import JsonRequest
from leo_flow.dashboard.symbolwise_replay import (
    RecordingSymbolwiseReplayDashboardProjectionV0_1,
)
from leo_flow.deployments import dashboard_v1
from leo_flow.services.bootstrap import AdapterBuildContext, Capability, Process
from leo_flow.services.config import DashboardServiceConfig, RuntimeConfig, SecretRef
from leo_flow.storage.filesystem import FileSystemBlobReader


def test_postgres_composition_uses_read_only_cas_and_authoritative_context(
    tmp_path: Path,
) -> None:
    context = AdapterBuildContext(
        Process.DASHBOARD,
        Capability.QUERY_PROJECTION,
        dashboard_v1.QUERY_PROJECTION_REF,
        {
            dashboard_v1.DATABASE_SECRET: "not-contacted",
            dashboard_v1.CAS_ROOT_SECRET: str(tmp_path / "objects"),
        },
    )

    repository = dashboard_v1._postgres_query_projection(context)

    assert isinstance(repository, PostgresDashboardRepository)
    projected = repository._symbolwise_replay
    assert isinstance(projected, RecordingSymbolwiseReplayDashboardProjectionV0_1)
    assert projected._evidence_context is repository._recording_evidence
    durable = projected._replay
    assert isinstance(durable, DurableRecordingStarlinkSymbolwiseReplayQueryV0_1)
    assert isinstance(durable._catalog, PostgresStarlinkSymbolwiseReplayRepositoryV0_1)
    assert durable._store._catalog is durable._catalog
    assert isinstance(durable._store._blobs, FileSystemBlobReader)
    assert not hasattr(durable._store._blobs, "put")
    assert not hasattr(repository, "enqueue")
    assert not hasattr(repository, "publish_starlink_symbolwise_replay")


def test_service_builder_wraps_v28_with_the_real_v29_handler(monkeypatch) -> None:
    captured = {}

    def capture_service(config, server, handler, *, diagnostics):  # type: ignore[no-untyped-def]
        captured.update(config=config, server=server, handler=handler, diagnostics=diagnostics)
        return "built"

    monkeypatch.setattr(dashboard_v1, "build_dashboard_service", capture_service)

    class Queries:
        def storage_health(self):
            return object()

        def recording_symbolwise_replay_dashboard(self, query):  # type: ignore[no-untyped-def]
            return {
                "recording_id": str(query.recording_id),
                "streams": [],
                "stream_count": 0,
                "window_count_per_stream": 600,
                "point_count": 0,
                "candidate_only": True,
                "calibrated_detection_count": None,
            }

    class Server:
        pass

    config = DashboardServiceConfig(
        1,
        "dashboard",
        RuntimeConfig(
            "dashboard-v29-composition",
            0.01,
            0.1,
            (SecretRef(dashboard_v1.SECRET_PROVIDER, "catalog-dsn"),),
        ),
        dashboard_v1.QUERY_PROJECTION_REF,
        dashboard_v1.SERVER_REF,
        "127.0.0.1",
        8080,
    )
    diagnostics = object()
    result = dashboard_v1._build_dashboard(
        config,
        {
            Capability.QUERY_PROJECTION: Queries(),
            Capability.DASHBOARD_SERVER: Server(),
        },
        diagnostics,  # type: ignore[arg-type]
    )

    assert result == "built"
    response = captured["handler"].handle(
        JsonRequest(
            "GET",
            "/api/v29/recordings/rec_composed/symbolwise-replay",
            {},
        )
    )
    assert response.status == 200
    assert json.loads(response.body) == {
        "calibrated_detection_count": None,
        "candidate_only": True,
        "point_count": 0,
        "recording_id": "rec_composed",
        "stream_count": 0,
        "streams": [],
        "window_count_per_stream": 600,
    }
