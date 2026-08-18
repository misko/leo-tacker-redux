from __future__ import annotations

import ast
import importlib
import io
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest

from leo_flow.contracts.dashboard import Page, StorageHealth
from leo_flow.dashboard import DashboardNotFound
from leo_flow.dashboard.api import JsonRequest
from leo_flow.deployments import dashboard_operator, dashboard_v1
from leo_flow.services import (
    AdapterBuildContext,
    BootstrapError,
    Capability,
    DashboardServiceConfig,
    Process,
    RuntimeConfig,
    SecretRef,
    assemble_service,
)
from leo_flow.services.cli import ExitCode, main
from tests.dashboard._recording_detail_fixtures import (
    RECORDING_ID,
    capture_detail,
    waterfall,
)


def _config(*, query_ref: str = dashboard_v1.QUERY_PROJECTION_REF):
    return DashboardServiceConfig(
        1,
        "dashboard",
        RuntimeConfig(
            "dashboard-1",
            0.01,
            0.1,
            (SecretRef(dashboard_v1.SECRET_PROVIDER, "catalog-dsn"),),
        ),
        query_ref,
        dashboard_v1.SERVER_REF,
        "127.0.0.1",
        8080,
    )


def test_dashboard_operator_pins_plugin_and_default_config_without_effects() -> None:
    observed: list[object] = []
    stdout = io.StringIO()
    stderr = io.StringIO()

    def run(argv=None, *, stdout, stderr):
        observed.extend((argv, stdout, stderr))
        return 17

    assert (
        dashboard_operator.main(
            ["--once"],
            stdout=stdout,
            stderr=stderr,
            service_runner=run,
        )
        == 17
    )
    assert observed == [
        [
            "--config",
            str(dashboard_operator.DEFAULT_CONFIG),
            "--plugin",
            dashboard_operator.PLUGIN_SPEC,
            "--once",
        ],
        stdout,
        stderr,
    ]


def test_dashboard_operator_forwards_only_config_and_process_mode(
    tmp_path: Path,
) -> None:
    observed: list[str] = []

    def run(argv=None, **_kwargs):
        assert argv is not None
        observed.extend(argv)
        return 0

    config = tmp_path / "dashboard.json"
    assert (
        dashboard_operator.main(
            ["--config", str(config), "--forever"], service_runner=run
        )
        == 0
    )
    assert observed == [
        "--config",
        str(config),
        "--plugin",
        "leo_flow.deployments.dashboard_v1:PLUGIN",
        "--forever",
    ]


def test_plugin_exports_only_exact_dashboard_capabilities() -> None:
    plugin = dashboard_v1.PLUGIN
    assert set(plugin.builders) == {Process.DASHBOARD}
    assert set(plugin.secret_providers) == {dashboard_v1.SECRET_PROVIDER}
    assert (
        plugin.manifest.factory(
            Process.DASHBOARD,
            Capability.QUERY_PROJECTION,
            dashboard_v1.QUERY_PROJECTION_REF,
        )
        is dashboard_v1._postgres_query_projection
    )
    assert (
        plugin.manifest.factory(
            Process.DASHBOARD, Capability.DASHBOARD_SERVER, dashboard_v1.SERVER_REF
        )
        is dashboard_v1._stdlib_loopback_server
    )
    assert (
        plugin.manifest.factory(
            Process.DASHBOARD,
            Capability.DASHBOARD_SERVER,
            dashboard_v1.REMOTE_SERVER_REF,
        )
        is dashboard_v1._stdlib_explicit_remote_server
    )
    with pytest.raises(BootstrapError, match="cannot resolve"):
        plugin.manifest.factory(Process.DASHBOARD, Capability.RADIO, "radio.pluto-v5")


def test_unknown_ref_fails_before_secret_read_or_socket_bind(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path / "does-not-exist"))
    with pytest.raises(BootstrapError, match="no exact query_projection"):
        assemble_service(
            _config(query_ref="dashboard.missing-v1"),
            dashboard_v1.PLUGIN,
            diagnostics=lambda event: None,
        )


def test_cli_unknown_ref_fails_before_credential_or_listener_effects(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path / "does-not-exist"))
    config = tmp_path / "dashboard.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "process": "dashboard",
                "runtime": {
                    "instance_id": "dashboard-1",
                    "poll_interval_s": 0.01,
                    "shutdown_timeout_s": 0.1,
                    "secret_refs": [
                        {
                            "provider": dashboard_v1.SECRET_PROVIDER,
                            "name": "catalog-dsn",
                        }
                    ],
                },
                "adapters": {
                    "query_projection_ref": "dashboard.missing-v1",
                    "server_ref": dashboard_v1.SERVER_REF,
                    "bind_host": "127.0.0.1",
                    "bind_port": 8080,
                },
            }
        ),
        encoding="utf-8",
    )
    stderr = io.StringIO()
    result = main(
        [
            "--config",
            str(config),
            "--plugin",
            "leo_flow.deployments.dashboard_v1:PLUGIN",
            "--once",
        ],
        stdout=io.StringIO(),
        stderr=stderr,
    )
    assert result is ExitCode.BOOTSTRAP
    assert stderr.getvalue() == '{"event":"bootstrap_error"}\n'


def test_complete_plugin_assembles_without_database_or_network_io(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "catalog-dsn").write_text("must-not-be-contacted", encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))

    psycopg = ModuleType("psycopg")

    def no_connect(*args, **kwargs):
        raise AssertionError("assembly must not connect to PostgreSQL")

    psycopg.connect = no_connect
    rows = ModuleType("psycopg.rows")
    rows.dict_row = object()
    postgres_adapter = ModuleType("leo_flow.adapters.dashboard_postgres")

    class FakeRepository:
        def __init__(self, connect, **kwargs) -> None:
            self.connect = connect
            self.kwargs = kwargs

    postgres_adapter.PostgresDashboardRepository = FakeRepository
    master_capture_adapter = ModuleType(
        "leo_flow.adapters.dashboard_master_capture_postgres"
    )

    class FakeMasterCaptureRepository:
        def __init__(self, connect, canary, **kwargs) -> None:
            self.connect = connect
            self.canary = canary
            self.kwargs = kwargs

    master_capture_adapter.PostgresMasterCaptureSnapshotRepositoryV0_1 = (
        FakeMasterCaptureRepository
    )
    doppler_snapshot_adapter = ModuleType(
        "leo_flow.adapters.dashboard_capture_doppler_postgres"
    )
    qam_snapshot_adapter = ModuleType(
        "leo_flow.adapters.dashboard_capture_qam_snapshot_postgres"
    )

    class FakeSnapshotRepository:
        def __init__(self, connect) -> None:
            self.connect = connect

    doppler_snapshot_adapter.PostgresCaptureDopplerSnapshotRepositoryV0_1 = (
        FakeSnapshotRepository
    )
    qam_snapshot_adapter.PostgresCaptureQamSnapshotRepositoryV0_1 = (
        FakeSnapshotRepository
    )
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)
    monkeypatch.setitem(
        sys.modules, "leo_flow.adapters.dashboard_postgres", postgres_adapter
    )
    monkeypatch.setitem(
        sys.modules,
        "leo_flow.adapters.dashboard_master_capture_postgres",
        master_capture_adapter,
    )
    monkeypatch.setitem(
        sys.modules,
        "leo_flow.adapters.dashboard_capture_doppler_postgres",
        doppler_snapshot_adapter,
    )
    monkeypatch.setitem(
        sys.modules,
        "leo_flow.adapters.dashboard_capture_qam_snapshot_postgres",
        qam_snapshot_adapter,
    )

    service = assemble_service(
        _config(), dashboard_v1.PLUGIN, diagnostics=lambda event: None
    )
    assert service.health().state.value == "stopped"


@dataclass
class _Server:
    events: list[str] = field(default_factory=list)
    bound: bool = False

    def preflight(self, bind_host: str, bind_port: int) -> None:
        assert (bind_host, bind_port) == ("127.0.0.1", 8080)
        self.events.append("bind")
        self.bound = True

    def serve_once(self, handler) -> bool:
        del handler
        self.events.append("serve")
        return False

    def close(self, timeout_s: float) -> None:
        assert timeout_s == 0.1
        self.events.append("close")
        self.bound = False


class _Queries:
    def __init__(self, events: list[str], *, failure: Exception | None = None) -> None:
        self._events = events
        self._failure = failure

    def storage_health(self):
        self._events.append("query")
        if self._failure is not None:
            raise self._failure
        return object()


class _Diagnostics:
    def emit(self, event) -> None:
        del event


def test_dashboard_preflight_binds_then_proves_query_capability_before_ready() -> None:
    server = _Server()
    service = dashboard_v1._build_dashboard(
        _config(),
        {
            Capability.QUERY_PROJECTION: _Queries(server.events),
            Capability.DASHBOARD_SERVER: server,
        },
        _Diagnostics(),
    )
    assert not service.health().ready
    assert not service.run_once()
    assert service.health().ready
    assert server.bound
    assert server.events == ["bind", "query", "serve"]
    service.shutdown()


def test_failed_query_preflight_closes_bound_listener_before_propagating() -> None:
    server = _Server()
    service = dashboard_v1._build_dashboard(
        _config(),
        {
            Capability.QUERY_PROJECTION: _Queries(
                server.events, failure=RuntimeError("role denied")
            ),
            Capability.DASHBOARD_SERVER: server,
        },
        _Diagnostics(),
    )
    with pytest.raises(RuntimeError, match="role denied"):
        service.run_once()
    assert not service.health().ready
    assert service.health().state.value == "failed"
    assert not server.bound
    assert server.events == ["bind", "query", "close"]


def test_normal_dashboard_composition_serves_v3_recordings_and_preserves_v1_v2() -> (
    None
):
    class Queries(_Queries):
        def storage_health(self):
            self._events.append("query")
            return StorageHealth(False, None, None)

        def recent_capture_batches(self, query, cursor=None):
            del query, cursor
            return Page((), None)

        def capture_batch(self, batch_id):
            raise AssertionError(f"unexpected exact query {batch_id}")

        def recording_capture_detail(self, recording_id):
            assert recording_id == RECORDING_ID
            return capture_detail()

        def recording_waterfall(self, recording_id):
            assert recording_id == RECORDING_ID
            return waterfall()

        def recording_starlink_suite(self, recording_id):
            assert recording_id == RECORDING_ID
            raise DashboardNotFound("suite projection absent")

        def recording_starlink_surrogate_null(self, query):
            assert query.recording_id == RECORDING_ID
            from tests.dashboard.test_starlink_surrogate_null_api import view

            return view(query)

        def recording_starlink_pilot_constellation(self, query):
            assert query.recording_id == RECORDING_ID
            from tests.dashboard.test_starlink_pilot_constellation_api import view

            return view(query)

    class CapturingServer(_Server):
        handler = None

        def serve_once(self, handler) -> bool:
            self.handler = handler
            return super().serve_once(handler)

    server = CapturingServer()
    service = dashboard_v1._build_dashboard(
        _config(),
        {
            Capability.QUERY_PROJECTION: Queries(server.events),
            Capability.DASHBOARD_SERVER: server,
        },
        _Diagnostics(),
    )
    assert not service.run_once()
    assert isinstance(server.handler, dashboard_v1.DashboardUiApplication)
    v2 = server.handler.handle(
        JsonRequest(
            "GET",
            "/api/capture-batches",
            {"start_utc_ns": "0", "stop_utc_ns": "1"},
        )
    )
    assert v2.status == 200 and v2.body == b'{"items":[],"next_cursor":null}'
    v3 = server.handler.handle(
        JsonRequest("GET", f"/api/recordings/{RECORDING_ID}", {})
    )
    assert v3.status == 200
    assert json.loads(v3.body)["recording_id"] == str(RECORDING_ID)
    waterfall_response = server.handler.handle(
        JsonRequest("GET", f"/api/recordings/{RECORDING_ID}/waterfall", {})
    )
    assert waterfall_response.status == 200
    assert json.loads(waterfall_response.body)["state"] == "complete"
    suite_response = server.handler.handle(
        JsonRequest("GET", f"/api/recordings/{RECORDING_ID}/starlink-suite", {})
    )
    assert suite_response.status == 404
    surrogate_response = server.handler.handle(
        JsonRequest(
            "GET",
            f"/api/recordings/{RECORDING_ID}/starlink-surrogate-null",
            {"methods": "glrt-32", "maximum_rows": "8"},
        )
    )
    assert surrogate_response.status == 200
    surrogate_payload = json.loads(surrogate_response.body)
    assert surrogate_payload["query"]["methods"] == ["glrt-32"]
    assert surrogate_payload["calibrated_detection_count"] is None
    constellation_response = server.handler.handle(
        JsonRequest(
            "GET",
            f"/api/recordings/{RECORDING_ID}/starlink-pilot-constellation",
            {"edges": "lower", "maximum_points_per_stream": "600"},
        )
    )
    assert constellation_response.status == 200
    constellation_payload = json.loads(constellation_response.body)
    assert constellation_payload["streams"][0]["edge"] == "lower"
    assert len(constellation_payload["streams"][0]["display_points"]) == 600
    v1 = server.handler.handle(JsonRequest("GET", "/api/storage-health", {}))
    assert v1.status == 200
    assert v1.body == b'{"available":false,"free_bytes":null,"total_bytes":null}'
    retired = server.handler.handle(
        JsonRequest("GET", f"/api/v3/recordings/{RECORDING_ID}", {})
    )
    assert retired.status == 410
    assert json.loads(retired.body)["error"]["code"] == "gone"
    service.shutdown()


def test_plugin_import_is_side_effect_free_and_does_not_import_psycopg() -> None:
    source = Path(dashboard_v1.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level_imports = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any(
        name == "psycopg" or name.startswith("psycopg.") for name in top_level_imports
    )

    sys.modules.pop("leo_flow.deployments.dashboard_v1", None)
    before = "psycopg" in sys.modules
    imported = importlib.import_module("leo_flow.deployments.dashboard_v1")
    assert imported.PLUGIN is not None
    assert ("psycopg" in sys.modules) is before


def test_missing_optional_postgres_dependency_has_an_actionable_error(
    monkeypatch,
) -> None:
    context = AdapterBuildContext(
        Process.DASHBOARD,
        Capability.QUERY_PROJECTION,
        dashboard_v1.QUERY_PROJECTION_REF,
        {dashboard_v1.DATABASE_SECRET: "not-contacted"},
    )
    real_import = __import__

    def reject_psycopg(name, *args, **kwargs):
        if name == "psycopg" or name.startswith("psycopg."):
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", reject_psycopg)
    with pytest.raises(
        dashboard_v1.DashboardRuntimeDependencyError, match="optional dependency"
    ):
        dashboard_v1._postgres_query_projection(context)


def test_postgres_projection_composes_read_only_analysis_product_readers(
    tmp_path: Path,
) -> None:
    from leo_flow.adapters.dashboard_postgres import PostgresDashboardRepository
    from leo_flow.analysis.recording.starlink_acquired_constellation_persistence import (
        DurableRecordingStarlinkAcquiredConstellationQueryV0_3,
    )
    from leo_flow.analysis.recording.starlink_pilot_constellation_persistence import (
        DurableRecordingStarlinkPilotConstellationQueryV0_1,
    )
    from leo_flow.analysis.recording.starlink_surrogate_null_persistence import (
        DurableRecordingStarlinkSurrogateNullQueryV0_1,
    )
    from leo_flow.storage.filesystem import FileSystemBlobReader

    context = AdapterBuildContext(
        Process.DASHBOARD,
        Capability.QUERY_PROJECTION,
        dashboard_v1.QUERY_PROJECTION_REF,
        {
            dashboard_v1.DATABASE_SECRET: "not-contacted",
            dashboard_v1.CAS_ROOT_SECRET: str(tmp_path / "objects"),
        },
    )

    projection = dashboard_v1._postgres_query_projection(context)

    assert isinstance(projection, PostgresDashboardRepository)
    assert isinstance(
        projection._surrogate_nulls,
        DurableRecordingStarlinkSurrogateNullQueryV0_1,
    )
    store = projection._surrogate_nulls._store
    assert isinstance(store._blobs, FileSystemBlobReader)
    assert not hasattr(store._blobs, "put")
    assert isinstance(
        projection._pilot_constellations,
        DurableRecordingStarlinkPilotConstellationQueryV0_1,
    )
    constellation_store = projection._pilot_constellations._store
    assert isinstance(constellation_store._blobs, FileSystemBlobReader)
    assert not hasattr(constellation_store._blobs, "put")
    assert isinstance(
        projection._acquired_qam,
        DurableRecordingStarlinkAcquiredConstellationQueryV0_3,
    )
    acquired_store = projection._acquired_qam._store
    assert isinstance(acquired_store._blobs, FileSystemBlobReader)
    assert not hasattr(acquired_store._blobs, "put")


def test_checked_gauss_dashboard_is_a_frozen_all_interface_read_only_unit() -> None:
    root = Path(__file__).resolve().parents[2]
    config = json.loads(
        (root / "deploy/dashboard-v1/dashboard.json").read_text(encoding="utf-8")
    )
    unit = (root / "deploy/dashboard-v1/leo-dashboard.service").read_text(
        encoding="utf-8"
    )

    assert config["adapters"] == {
        "query_projection_ref": dashboard_v1.QUERY_PROJECTION_REF,
        "server_ref": dashboard_v1.REMOTE_SERVER_REF,
        "bind_host": "0.0.0.0",
        "bind_port": 8090,
    }
    assert (
        "ExecStart=/usr/bin/flock --nonblock "
        "/run/leo-flow-dashboard/supervisor.lock "
        "/opt/leo-flow/bin/leo-dashboard "
        "--config /etc/leo-flow/dashboard.json --forever"
    ) in unit
    for directive in (
        "DynamicUser=yes",
        "LoadCredential=catalog-dsn:/etc/leo-flow/secrets/dashboard-catalog-dsn",
        "LoadCredential=analysis-cas-root:/etc/leo-flow/secrets/dashboard-analysis-cas-root",
        "ReadOnlyPaths=/var/lib/leo-flow/objects",
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "UMask=0077",
    ):
        assert directive in unit
    for forbidden in (
        "/home/",
        "PYTHONPATH=",
        "Environment=",
        "SupplementaryGroups=",
        "StateDirectory=",
        "ReadWritePaths=",
        "BindPaths=",
        "cas_root",
        "capture-spool",
    ):
        assert forbidden not in unit
