from __future__ import annotations

import errno
import fcntl
import io
import json
import os
import shutil
import socket
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from leo_flow.deployments import site_readiness
from leo_flow.deployments.site_readiness import (
    SiteReadinessError,
    load_manifest,
    main,
    qualify_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "deploy" / "site-readiness-v1"
EXAMPLE = BUNDLE / "site-readiness.example.json"
SCHEMA = BUNDLE / "site-readiness.schema.json"


def _candidate(
    payload: bytes = b'{"ready":true}\n',
) -> site_readiness._CapturedCandidate:
    return site_readiness._CapturedCandidate(
        payload=payload, text=payload.decode("utf-8")
    )


def test_captured_loader_uses_linux_uapi_fallbacks_when_exports_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "F_ADD_SEALS",
        "F_GET_SEALS",
        "F_SEAL_SEAL",
        "F_SEAL_SHRINK",
        "F_SEAL_GROW",
        "F_SEAL_WRITE",
    ):
        monkeypatch.delattr(site_readiness.fcntl, name, raising=False)

    observed = site_readiness._load_captured(
        _candidate(), lambda path: path.read_bytes()
    )

    assert observed == b'{"ready":true}\n'


def test_captured_loader_applies_exact_seal_mask_before_loader() -> None:
    def inspect_and_try_write(path: Path) -> int:
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        try:
            observed = fcntl.fcntl(fd, 1034)
            with pytest.raises(OSError) as error:
                os.write(fd, b"mutation")
            assert error.value.errno == errno.EPERM
            return observed
        finally:
            os.close(fd)

    assert site_readiness._load_captured(_candidate(), inspect_and_try_write) == 15


@pytest.mark.parametrize("failed_command", [1033, 1034])
def test_captured_loader_fails_closed_when_seal_operations_fail(
    failed_command: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fcntl = fcntl.fcntl
    loader_called = False

    def fail_operation(fd: int, command: int, *args: int) -> int:
        if command == failed_command:
            raise OSError(errno.EINVAL, "untrusted platform detail")
        result = real_fcntl(fd, command, *args)
        assert isinstance(result, int)
        return result

    def loader(_path: Path) -> object:
        nonlocal loader_called
        loader_called = True
        return object()

    monkeypatch.setattr(site_readiness.fcntl, "fcntl", fail_operation)

    with pytest.raises(
        SiteReadinessError, match="^immutable candidate staging is unavailable$"
    ):
        site_readiness._load_captured(_candidate(), loader)
    assert loader_called is False


def test_captured_loader_rejects_incomplete_seal_mask_before_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_fcntl = fcntl.fcntl
    loader_called = False

    def report_incomplete(fd: int, command: int, *args: int) -> int:
        if command == 1034:
            return 7
        result = real_fcntl(fd, command, *args)
        assert isinstance(result, int)
        return result

    def loader(_path: Path) -> object:
        nonlocal loader_called
        loader_called = True
        return object()

    monkeypatch.setattr(site_readiness.fcntl, "fcntl", report_incomplete)

    with pytest.raises(
        SiteReadinessError, match="^immutable candidate staging is unavailable$"
    ):
        site_readiness._load_captured(_candidate(), loader)
    assert loader_called is False


def test_captured_loader_rejects_zero_byte_write_before_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader_called = False

    def loader(_path: Path) -> object:
        nonlocal loader_called
        loader_called = True
        return object()

    monkeypatch.setattr(site_readiness.os, "write", lambda *_args: 0)

    with pytest.raises(
        SiteReadinessError, match="^immutable candidate staging is unavailable$"
    ):
        site_readiness._load_captured(_candidate(), loader)
    assert loader_called is False


def test_captured_loader_sanitizes_unsupported_memfd_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader_called = False

    def unsupported(*_args: object, **_kwargs: object) -> int:
        raise OSError(errno.ENOSYS, "untrusted platform detail")

    def loader(_path: Path) -> object:
        nonlocal loader_called
        loader_called = True
        return object()

    monkeypatch.setattr(site_readiness.os, "memfd_create", unsupported)

    with pytest.raises(
        SiteReadinessError, match="^immutable candidate staging is unavailable$"
    ):
        site_readiness._load_captured(_candidate(), loader)
    assert loader_called is False


def _artifact_values(document: dict[str, object]) -> Iterator[dict[str, object]]:
    inputs = document["inputs"]
    assert isinstance(inputs, dict)
    for group in ("capture", "dashboard", "ephemeris", "health"):
        value = inputs[group]
        assert isinstance(value, dict)
        artifact = value["config"]
        assert isinstance(artifact, dict)
        yield artifact
    storage = inputs["storage"]
    assert isinstance(storage, dict)
    for key in ("capacity_config", "offhost_config"):
        artifact = storage[key]
        assert isinstance(artifact, dict)
        yield artifact
    analysis = inputs["analysis"]
    assert isinstance(analysis, dict)
    workers = analysis["workers"]
    assert isinstance(workers, list)
    for worker in workers:
        assert isinstance(worker, dict)
        artifact = worker["config"]
        assert isinstance(artifact, dict)
        yield artifact
    units = inputs["units"]
    assert isinstance(units, list)
    for artifact in units:
        assert isinstance(artifact, dict)
        yield artifact


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _digest(path: Path) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _complete_bundle(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["site_id"] = "site-a"
    postgres = document["postgres"]
    assert isinstance(postgres, dict)
    endpoint = postgres["endpoint"]
    assert isinstance(endpoint, dict)
    endpoint.update(host="postgres.site.test", database="leo_flow")
    dashboard_policy = document["dashboard_policy"]
    assert isinstance(dashboard_policy, dict)
    dashboard_policy.update(
        public_origin="https://dashboard.site.test/",
        auth_policy_ref="auth.site-operators-v1",
        tls_certificate_ref="pki.dashboard-site-test-cert-v1",
        tls_private_key_ref="pki.dashboard-site-test-key-v1",
    )
    operations = document["operations"]
    assert isinstance(operations, dict)
    operations["alert_route_ref"] = "alerts.site-operations-v1"

    for artifact in _artifact_values(document):
        source = artifact["source_path"]
        assert isinstance(source, str)
        destination = tmp_path / source
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / source, destination)

    offhost = {
        "schema_id": "org.leo-flow.offhost-qualification",
        "schema_version": "0.3",
        "station_id": "station-a",
        "cas": {
            "root": "/var/lib/leo-flow/objects",
            "mount_source": "storage.site.test:/leo-flow-cas",
            "filesystem_type": "nfs4",
            "group_name": "leo-flow-cas",
            "mount_root": "/",
        },
        "migration_directory": "/opt/leo-flow/migrations",
        "credential_names": {
            "leo_capture": "capture-catalog-dsn",
            "leo_analysis": "analysis-catalog-dsn",
            "leo_dashboard": "dashboard-catalog-dsn",
            "postgres_audit": "postgres-audit-dsn",
        },
        "postgres": {
            "database_name": "leo_flow",
            "database_owner": "leo_catalog_owner",
            "server_major": 16,
            "system_identifier": "7612345678901234567",
            "migration_head": "0032_campaign_online_analysis.sql",
            "login_names": {
                "leo_capture": "leo_capture_station_login",
                "leo_analysis": "leo_analysis_station_login",
                "leo_dashboard": "leo_dashboard_station_login",
                "postgres_audit": "leo_catalog_audit_login",
            },
        },
        "pipeline": None,
    }
    _write_json(
        tmp_path / "deploy/offhost-qualification/qualification.example.json",
        offhost,
    )
    capacity = {
        "schema_version": 1,
        "fail_on": "warn",
        "thresholds": {
            "warn_free_bytes": 107374182400,
            "critical_free_bytes": 53687091200,
            "warn_free_fraction": 0.2,
            "critical_free_fraction": 0.1,
            "warn_seconds_to_full": 86400,
            "critical_seconds_to_full": 21600,
        },
        "roots": [
            {
                "name": "authoritative-cas",
                "path": "/var/lib/leo-flow/objects",
                "estimated_bytes_per_second": 1048576,
            }
        ],
    }
    _write_json(
        tmp_path / "deploy/storage-capacity/capacity.example.json",
        capacity,
    )
    for artifact in _artifact_values(document):
        source = artifact["source_path"]
        assert isinstance(source, str)
        artifact["sha256"] = _digest(tmp_path / source)
    manifest_path = tmp_path / "site.json"
    _write_json(manifest_path, document)
    return manifest_path, document


def _gate(receipt: Mapping[str, object], name: str) -> Mapping[str, object]:
    gates = receipt["gates"]
    assert isinstance(gates, list)
    for gate in gates:
        if isinstance(gate, dict) and gate.get("name") == name:
            return gate
    raise AssertionError(f"missing gate {name}")


def test_example_and_schema_are_closed_operator_templates() -> None:
    manifest = load_manifest(EXAMPLE)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    assert manifest.unresolved_fields
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_id"]["const"] == ("org.leo-flow.site-readiness")
    assert "password" not in json.dumps(schema).lower()
    assert "dsn" not in json.dumps(schema).lower()


def test_complete_bundle_passes_without_external_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path, _ = _complete_bundle(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("site readiness attempted external access")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(
        "leo_flow.adapters.systemd_credentials.SystemdCredentialProvider.resolve",
        forbidden,
    )
    manifest = load_manifest(manifest_path)
    first = qualify_manifest(manifest, tmp_path)
    second = qualify_manifest(manifest, tmp_path)

    assert first == second
    assert first["status"] == "pass"
    assert first["mode"] == "offline-no-contact"
    external_access = first["external_access"]
    assert isinstance(external_access, dict)
    assert set(external_access.values()) == {False}
    assert _gate(first, "offhost.no_contact_preflight")["passed"] is True
    assert _gate(first, "wiring.target.workers_and_timers")["passed"] is True
    qualified = first["qualified_inputs"]
    assert isinstance(qualified, dict)
    storage = qualified["storage"]
    assert isinstance(storage, dict)
    assert storage["mount_source"] == "storage.site.test:/leo-flow-cas"
    assert storage["capacity_roots"] == ["/var/lib/leo-flow/objects"]
    plan = first["install_plan"]
    assert isinstance(plan, list)
    assert len(plan) == 17
    assert [item["destination_path"] for item in plan] == sorted(
        item["destination_path"] for item in plan
    )


def test_dashboard_remote_bind_requires_the_explicit_remote_adapter(
    tmp_path: Path,
) -> None:
    manifest_path, document = _complete_bundle(tmp_path)
    inputs = document["inputs"]
    assert isinstance(inputs, dict)
    dashboard = inputs["dashboard"]
    assert isinstance(dashboard, dict)
    artifact = dashboard["config"]
    assert isinstance(artifact, dict)
    source = artifact["source_path"]
    assert isinstance(source, str)
    config_path = tmp_path / source
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["adapters"]["server_ref"] = "dashboard.stdlib-loopback-http-v1"
    _write_json(config_path, config)
    artifact["sha256"] = _digest(config_path)
    _write_json(manifest_path, document)

    receipt = qualify_manifest(load_manifest(manifest_path), tmp_path)

    assert receipt["status"] == "fail"
    assert _gate(receipt, "dashboard.explicit_bind_policy")["passed"] is False


def test_changed_candidate_and_cross_config_drift_fail_closed(tmp_path: Path) -> None:
    manifest_path, document = _complete_bundle(tmp_path)
    manifest = load_manifest(manifest_path)
    (tmp_path / manifest.capture_config.source_path).write_text(
        "{}\n", encoding="utf-8"
    )

    receipt = qualify_manifest(manifest, tmp_path)

    assert receipt["status"] == "fail"
    assert _gate(receipt, "artifact.capture.config.pinned")["passed"] is False
    assert _gate(receipt, "config.capture.strict")["passed"] is False

    manifest_path, document = _complete_bundle(tmp_path / "drift")
    inputs = document["inputs"]
    assert isinstance(inputs, dict)
    capture = inputs["capture"]
    assert isinstance(capture, dict)
    capture["radio_ref"] = "radio.different-reviewed-identity-v1"
    _write_json(manifest_path, document)
    receipt = qualify_manifest(load_manifest(manifest_path), tmp_path / "drift")
    assert receipt["status"] == "fail"
    assert _gate(receipt, "capture.identity")["passed"] is False


def test_multiple_workers_require_distinct_configs_and_exact_target_roster(
    tmp_path: Path,
) -> None:
    manifest_path, document = _complete_bundle(tmp_path)
    inputs = document["inputs"]
    assert isinstance(inputs, dict)
    analysis = inputs["analysis"]
    assert isinstance(analysis, dict)
    workers = analysis["workers"]
    assert isinstance(workers, list)
    second_source = "deploy/site-readiness-v1/analysis-worker-2.json"
    second_config = json.loads(
        (tmp_path / "deploy/offline-analysis-v1/analysis.json").read_text(
            encoding="utf-8"
        )
    )
    second_config["runtime"]["instance_id"] = "station-a-offline-analysis-2"
    _write_json(tmp_path / second_source, second_config)
    workers.append(
        {
            "instance": "worker-2",
            "config": {
                "source_path": second_source,
                "destination_path": "/etc/leo-flow/analysis-worker-2.json",
                "sha256": _digest(tmp_path / second_source),
            },
        }
    )
    units = inputs["units"]
    assert isinstance(units, list)
    target = next(
        unit
        for unit in units
        if isinstance(unit, dict) and unit.get("unit") == "leo-flow.target"
    )
    target_source = target["source_path"]
    assert isinstance(target_source, str)
    target_path = tmp_path / target_source
    target_path.write_text(
        target_path.read_text(encoding="utf-8").replace(
            "leo-offline-analysis@worker-1.service leo-dashboard.service",
            "leo-offline-analysis@worker-1.service "
            "leo-offline-analysis@worker-2.service leo-dashboard.service",
            1,
        ),
        encoding="utf-8",
    )
    target["sha256"] = _digest(target_path)
    _write_json(manifest_path, document)

    receipt = qualify_manifest(load_manifest(manifest_path), tmp_path)

    assert receipt["status"] == "pass"
    qualified = receipt["qualified_inputs"]
    assert isinstance(qualified, dict)
    worker_receipt = qualified["analysis"]
    assert isinstance(worker_receipt, dict)
    assert worker_receipt["workers"] == ["worker-1", "worker-2"]


def test_manifest_rejects_secret_shaped_or_incomplete_fields(tmp_path: Path) -> None:
    manifest_path, document = _complete_bundle(tmp_path)
    postgres = document["postgres"]
    assert isinstance(postgres, dict)
    postgres["dsn"] = "postgresql://user:secret@postgres.site.test/leo_flow"
    _write_json(manifest_path, document)

    with pytest.raises(SiteReadinessError, match="fields are not exact"):
        load_manifest(manifest_path)


def test_cli_emits_exact_plan_but_writes_or_installs_nothing(tmp_path: Path) -> None:
    manifest_path, _ = _complete_bundle(tmp_path)
    output = io.StringIO()
    errors = io.StringIO()

    assert (
        main(
            [
                "--manifest",
                str(manifest_path),
                "--repository-root",
                str(tmp_path),
            ],
            stdout=output,
            stderr=errors,
        )
        == 0
    )
    receipt = json.loads(output.getvalue())
    assert receipt["status"] == "pass"
    assert errors.getvalue() == ""
    assert not (tmp_path / "etc").exists()
    assert not (tmp_path / "var").exists()


def test_pinned_units_pass_isolated_static_systemd_verification(tmp_path: Path) -> None:
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd-analyze is not installed")
    manifest_path, _ = _complete_bundle(tmp_path / "bundle")
    manifest = load_manifest(manifest_path)
    unit_root = tmp_path / "units"
    unit_root.mkdir()
    materialized: list[Path] = []
    for unit in manifest.units:
        source = tmp_path / "bundle" / unit.artifact.source_path
        destination = unit_root / unit.unit
        content = source.read_text(encoding="utf-8").replace(
            "ExecStart=/opt/leo-flow/bin/python",
            "ExecStart=/bin/true",
        )
        destination.write_text(content, encoding="utf-8")
        materialized.append(destination)

    vendor_paths = [
        path
        for path in (Path("/usr/lib/systemd/system"), Path("/lib/systemd/system"))
        if path.is_dir()
    ]
    environment = dict(os.environ)
    environment["SYSTEMD_UNIT_PATH"] = ":".join(
        [str(unit_root), *(str(path) for path in vendor_paths)]
    )
    environment["SYSTEMD_COLORS"] = "0"
    environment["SYSTEMD_PAGER"] = "cat"
    result = subprocess.run(
        [analyzer, "verify", *(str(path) for path in materialized)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
