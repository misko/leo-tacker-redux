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

from leo_flow.deployments import dashboard_v1
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
        def __init__(self, connect) -> None:
            self.connect = connect

    postgres_adapter.PostgresDashboardRepository = FakeRepository
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)
    monkeypatch.setitem(
        sys.modules, "leo_flow.adapters.dashboard_postgres", postgres_adapter
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
