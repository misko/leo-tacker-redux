from __future__ import annotations

import io
import json
import sys
from dataclasses import dataclass
from types import ModuleType

import pytest

from leo_flow.services import (
    AdapterManifest,
    BootstrapError,
    Capability,
    CaptureServiceConfig,
    DeploymentPlugin,
    JsonLineDiagnosticSink,
    Process,
    RuntimeConfig,
    SecretRef,
    assemble_service,
    build_capture_service,
)
from leo_flow.services.cli import ExitCode, main

CAPTURE_REFS = {
    Capability.PLAN_SOURCE: "plans.pg-v1",
    Capability.RADIO: "radio.pluto-v5",
    Capability.CAPTURE_PREFLIGHT: "preflight.v5-v1",
    Capability.RECORDING_WRITER: "writer.sigmf-v1",
    Capability.SPOOL: "spool.sqlite-v1",
    Capability.RECORDING_PUBLISHER: "publisher.catalog-v1",
}


@dataclass
class SecretStore:
    values: dict[str, str]
    calls: int = 0

    def resolve(self, name: str) -> str:
        self.calls += 1
        return self.values[name]


class Cycle:
    def __init__(self) -> None:
        self.started = False
        self.closed = False

    def preflight(self) -> None:
        self.started = True

    def capture_and_publish_once(self) -> bool:
        return True

    def close(self, timeout_s: float) -> None:
        assert timeout_s == 0.1
        self.closed = True


def config(*, radio_ref: str = "radio.pluto-v5") -> CaptureServiceConfig:
    return CaptureServiceConfig(
        1,
        "capture",
        RuntimeConfig(
            "capture-1", 0.01, 0.1, (SecretRef("credential", "catalog-dsn"),)
        ),
        CAPTURE_REFS[Capability.PLAN_SOURCE],
        radio_ref,
        CAPTURE_REFS[Capability.CAPTURE_PREFLIGHT],
        CAPTURE_REFS[Capability.RECORDING_WRITER],
        CAPTURE_REFS[Capability.SPOOL],
        CAPTURE_REFS[Capability.RECORDING_PUBLISHER],
    )


def plugin(
    *, factory_calls: list[Capability], secret_store: SecretStore | None = None
) -> tuple[DeploymentPlugin, Cycle]:
    def factory(context):
        factory_calls.append(context.capability)
        assert context.process is Process.CAPTURE
        assert context.reference == CAPTURE_REFS[context.capability]
        assert context.secrets[SecretRef("credential", "catalog-dsn")] == "dsn"
        with pytest.raises(TypeError):
            context.secrets[SecretRef("credential", "other")] = "bad"
        return context.capability.value

    manifest = AdapterManifest(
        {
            Process.CAPTURE: {
                capability: {reference: factory}
                for capability, reference in CAPTURE_REFS.items()
            }
        }
    )
    cycle = Cycle()

    def builder(service_config, adapters, diagnostics):
        assert isinstance(service_config, CaptureServiceConfig)
        assert set(adapters) == set(CAPTURE_REFS)
        return build_capture_service(service_config, cycle, diagnostics=diagnostics)

    return (
        DeploymentPlugin(
            manifest,
            {"credential": secret_store or SecretStore({"catalog-dsn": "dsn"})},
            {Process.CAPTURE: builder},
        ),
        cycle,
    )


def test_complete_manifest_assembles_only_selected_process_capabilities() -> None:
    calls: list[Capability] = []
    deployment, cycle = plugin(factory_calls=calls)
    service = assemble_service(
        config(), deployment, diagnostics=JsonLineDiagnosticSink(io.StringIO())
    )
    assert calls == list(CAPTURE_REFS)
    assert not cycle.started
    assert service.run_once()
    assert cycle.started
    service.shutdown()
    assert cycle.closed


@pytest.mark.parametrize(
    ("process", "foreign"),
    [
        (Process.CAPTURE, Capability.JOB_REPOSITORY),
        (Process.ANALYSIS, Capability.RADIO),
        (Process.DASHBOARD, Capability.RECORDING_WRITER),
    ],
)
def test_process_cannot_resolve_another_process_capability(
    process: Process, foreign: Capability
) -> None:
    manifest = AdapterManifest({process: {}})
    with pytest.raises(BootstrapError, match="cannot resolve"):
        manifest.factory(process, foreign, "foreign.v1")


def test_manifest_rejects_cross_process_registry_at_construction() -> None:
    with pytest.raises(BootstrapError, match="forbidden capabilities"):
        AdapterManifest(
            {Process.CAPTURE: {Capability.JOB_REPOSITORY: {"jobs.v1": object}}}
        )


def test_manifest_copies_registries_into_immutable_exact_selection() -> None:
    def factory(context):
        return context.reference

    registry = {"radio.pluto-v5": factory}
    manifest = AdapterManifest({Process.CAPTURE: {Capability.RADIO: registry}})
    registry["radio.pluto-v5"] = object
    assert (
        manifest.factory(Process.CAPTURE, Capability.RADIO, "radio.pluto-v5") is factory
    )


def test_missing_factory_fails_before_secret_or_adapter_resolution() -> None:
    calls: list[Capability] = []
    store = SecretStore({"catalog-dsn": "dsn"})
    deployment, _ = plugin(factory_calls=calls, secret_store=store)
    with pytest.raises(BootstrapError, match="no exact radio"):
        assemble_service(
            config(radio_ref="radio.missing-v1"),
            deployment,
            diagnostics=lambda event: None,
        )
    assert store.calls == 0
    assert calls == []


def test_missing_secret_provider_fails_before_adapter_factories() -> None:
    calls: list[Capability] = []
    deployment, _ = plugin(factory_calls=calls)
    deployment = DeploymentPlugin(deployment.manifest, {}, deployment.builders)
    with pytest.raises(BootstrapError, match="secret provider"):
        assemble_service(config(), deployment, diagnostics=lambda event: None)
    assert calls == []


@pytest.mark.parametrize("reference", ["radio.latest", "radio.DEFAULT.v1"])
def test_ambient_adapter_aliases_are_never_resolved(reference: str) -> None:
    deployment, _ = plugin(factory_calls=[])
    with pytest.raises(BootstrapError, match="aliases"):
        assemble_service(
            config(radio_ref=reference), deployment, diagnostics=lambda event: None
        )


def test_cli_runs_explicit_plugin_once_with_jsonl_and_clean_close(
    tmp_path, monkeypatch
) -> None:
    calls: list[Capability] = []
    deployment, cycle = plugin(factory_calls=calls)
    module = ModuleType("test_deployment_plugin")
    module.DEPLOYMENT = deployment
    monkeypatch.setitem(sys.modules, module.__name__, module)
    path = tmp_path / "capture.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "process": "capture",
                "runtime": {
                    "instance_id": "capture-1",
                    "poll_interval_s": 0.01,
                    "shutdown_timeout_s": 0.1,
                    "secret_refs": [{"provider": "credential", "name": "catalog-dsn"}],
                },
                "adapters": {
                    "plan_source_ref": CAPTURE_REFS[Capability.PLAN_SOURCE],
                    "radio_ref": CAPTURE_REFS[Capability.RADIO],
                    "preflight_ref": CAPTURE_REFS[Capability.CAPTURE_PREFLIGHT],
                    "recording_writer_ref": CAPTURE_REFS[Capability.RECORDING_WRITER],
                    "spool_ref": CAPTURE_REFS[Capability.SPOOL],
                    "recording_publisher_ref": CAPTURE_REFS[
                        Capability.RECORDING_PUBLISHER
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    result = main(
        [
            "--config",
            str(path),
            "--plugin",
            "test_deployment_plugin:DEPLOYMENT",
            "--once",
        ],
        stdout=stdout,
        stderr=stderr,
    )
    assert result is ExitCode.OK
    assert cycle.closed
    assert stderr.getvalue() == ""
    events = [json.loads(line)["event"] for line in stdout.getvalue().splitlines()]
    assert events == ["starting", "ready", "unit_completed", "draining", "stopped"]


def test_cli_failure_is_deterministic_and_does_not_echo_sensitive_refs(
    tmp_path,
) -> None:
    path = tmp_path / "capture.json"
    path.write_text("{}", encoding="utf-8")
    stderr = io.StringIO()
    result = main(
        ["--config", str(path), "--plugin", "secret.module:dsn"],
        stdout=io.StringIO(),
        stderr=stderr,
    )
    assert result is ExitCode.USAGE_OR_CONFIG
    assert stderr.getvalue() == '{"event":"configuration_error"}\n'
    assert "secret" not in stderr.getvalue()
