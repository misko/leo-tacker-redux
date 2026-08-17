"""Fail-closed, no-contact qualification of a site deployment bundle.

The checker reads only the manifest and its pinned local candidate files.  It
does not resolve credentials, inspect mounts, contact PostgreSQL or a radio,
invoke systemd, or install anything.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, TextIO, TypeVar
from urllib.parse import urlsplit

from leo_flow.deployments.ephemeris_provider_canary import load_canary_config
from leo_flow.deployments.systemd_health import load_config as load_health_config
from leo_flow.deployments.v5_scan import (
    CAPTURE_IDENTITY,
    PLAN_ID,
    PLAN_SOURCE_REF,
    RADIO_ID,
    RADIO_REF,
    SCAN_PLAN_DIGEST,
)
from leo_flow.maintenance.capacity import load_configuration
from leo_flow.qualification.offhost import (
    QualificationConfig,
    build_preflight_report,
)
from leo_flow.qualification.offhost import (
    load_config as load_offhost_config,
)
from leo_flow.services.config import load_service_config

SCHEMA_ID: Final = "org.leo-flow.site-readiness"
SCHEMA_VERSION: Final = "0.1"
RECEIPT_SCHEMA_ID: Final = "org.leo-flow.site-readiness-receipt"
RECEIPT_SCHEMA_VERSION: Final = "0.1"
MAX_MANIFEST_BYTES: Final = 256 * 1024
MAX_CANDIDATE_BYTES: Final = 16 * 1024 * 1024
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
_PLUGIN_REF = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    r":[A-Za-z_][A-Za-z0-9_]*$"
)
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_PLACEHOLDERS: Final = ("REPLACE_WITH_", "<REPLACE_", "EXAMPLE.INVALID")
_ROLES: Final = (
    "leo_capture",
    "leo_analysis",
    "leo_dashboard",
    "postgres_audit",
)
_REQUIRED_UNITS: Final = {
    "leo-flow.target",
    "leo-flow-health.service",
    "leo-flow-health.timer",
    "leo-v5-scan.service",
    "leo-offline-analysis@.service",
    "leo-dashboard.service",
    "leo-storage-capacity.service",
    "leo-storage-capacity.timer",
    "leo-ephemeris-provider-canary.service",
    "leo-ephemeris-provider-canary.timer",
}
_LINUX_F_ADD_SEALS: Final = 1033
_LINUX_F_GET_SEALS: Final = 1034
_LINUX_F_SEAL_SEAL: Final = 0x0001
_LINUX_F_SEAL_SHRINK: Final = 0x0002
_LINUX_F_SEAL_GROW: Final = 0x0004
_LINUX_F_SEAL_WRITE: Final = 0x0008
_LINUX_MFD_CLOEXEC: Final = 0x0001
_LINUX_MFD_ALLOW_SEALING: Final = 0x0002
_DASHBOARD_LOOPBACK_SERVER_REF: Final = "dashboard.stdlib-loopback-http-v1"
_DASHBOARD_REMOTE_SERVER_REF: Final = "dashboard.stdlib-explicit-remote-http-v1"
_T = TypeVar("_T")


class SiteReadinessError(RuntimeError):
    """The site manifest could not be safely parsed or qualified."""


@dataclass(frozen=True)
class _CapturedCandidate:
    payload: bytes
    text: str


def _linux_integer_constant(namespace: object, name: str, fallback: int) -> int:
    """Return an exported Linux UAPI constant or its stable numeric value."""

    value = getattr(namespace, name, fallback)
    if type(value) is not int or value < 0:
        raise SiteReadinessError("immutable candidate staging is unavailable")
    return value


def _open_sealed_candidate(payload: bytes) -> int:
    """Copy bytes to a Linux memfd and prove that its complete seal is active."""

    fd: int | None = None
    completed = False
    try:
        cloexec = _linux_integer_constant(os, "MFD_CLOEXEC", _LINUX_MFD_CLOEXEC)
        allow_sealing = _linux_integer_constant(
            os, "MFD_ALLOW_SEALING", _LINUX_MFD_ALLOW_SEALING
        )
        add_seals = _linux_integer_constant(fcntl, "F_ADD_SEALS", _LINUX_F_ADD_SEALS)
        get_seals = _linux_integer_constant(fcntl, "F_GET_SEALS", _LINUX_F_GET_SEALS)
        seal_mask = (
            _linux_integer_constant(fcntl, "F_SEAL_SEAL", _LINUX_F_SEAL_SEAL)
            | _linux_integer_constant(fcntl, "F_SEAL_SHRINK", _LINUX_F_SEAL_SHRINK)
            | _linux_integer_constant(fcntl, "F_SEAL_GROW", _LINUX_F_SEAL_GROW)
            | _linux_integer_constant(fcntl, "F_SEAL_WRITE", _LINUX_F_SEAL_WRITE)
        )
        fd = os.memfd_create("leo-site-readiness", cloexec | allow_sealing)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0 or written > len(view):
                raise SiteReadinessError("immutable candidate staging is unavailable")
            view = view[written:]
        fcntl.fcntl(fd, add_seals, seal_mask)
        observed_seals = fcntl.fcntl(fd, get_seals)
        if type(observed_seals) is not int or observed_seals != seal_mask:
            raise SiteReadinessError("immutable candidate staging is unavailable")
        completed = True
        return fd
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        raise SiteReadinessError("immutable candidate staging is unavailable") from None
    finally:
        if fd is not None and not completed:
            try:
                os.close(fd)
            except OSError:
                pass


def _capture_candidate(root: Path, source_path: str) -> _CapturedCandidate:
    """Read one bounded regular file without following any path-component links."""

    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    parts = PurePosixPath(source_path).parts
    directory_fd = os.open(root, directory_flags)
    opened_directories: list[int] = [directory_fd]
    file_fd: int | None = None
    try:
        for part in parts[:-1]:
            directory_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            opened_directories.append(directory_fd)
        file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise SiteReadinessError("candidate is not a regular file")
        if before.st_size > MAX_CANDIDATE_BYTES:
            raise SiteReadinessError("candidate exceeds its size bound")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_CANDIDATE_BYTES:
            chunk = os.read(file_fd, min(1024 * 1024, MAX_CANDIDATE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > MAX_CANDIDATE_BYTES:
            raise SiteReadinessError("candidate exceeds its size bound")
        after = os.fstat(file_fd)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after:
            raise SiteReadinessError("candidate changed while it was captured")
        payload = b"".join(chunks)
        return _CapturedCandidate(payload=payload, text=payload.decode("utf-8"))
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for opened_fd in reversed(opened_directories):
            os.close(opened_fd)


def _load_captured(candidate: _CapturedCandidate, loader: Callable[[Path], _T]) -> _T:
    """Run an existing strict loader against the immutable captured bytes."""

    fd = _open_sealed_candidate(candidate.payload)
    try:
        return loader(Path(f"/proc/self/fd/{fd}"))
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Artifact:
    source_path: str
    destination_path: Path
    sha256: str

    def __post_init__(self) -> None:
        source = PurePosixPath(self.source_path)
        if (
            source.is_absolute()
            or not source.parts
            or ".." in source.parts
            or self.source_path in {"", "."}
            or source.as_posix() != self.source_path
            or not _is_exact_absolute_path(str(self.destination_path))
            or not _DIGEST.fullmatch(self.sha256)
        ):
            raise SiteReadinessError("artifact paths and digest must be exact")


@dataclass(frozen=True)
class UnitArtifact:
    unit: str
    artifact: Artifact

    def __post_init__(self) -> None:
        if self.unit not in _REQUIRED_UNITS:
            raise SiteReadinessError("site unit inventory is unsupported")


@dataclass(frozen=True)
class AnalysisWorker:
    instance: str
    config: Artifact

    def __post_init__(self) -> None:
        if not _TOKEN.fullmatch(self.instance) or self.instance.startswith("-"):
            raise SiteReadinessError("analysis worker instance is invalid")


@dataclass(frozen=True)
class CredentialBinding:
    name: str
    source_path: Path

    def __post_init__(self) -> None:
        if not _TOKEN.fullmatch(self.name) or not _is_exact_absolute_path(
            str(self.source_path)
        ):
            raise SiteReadinessError("credential binding is invalid")


@dataclass(frozen=True)
class Gate:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class SiteManifest:
    site_id: str
    capture_config: Artifact
    capture_plan_source_ref: str
    capture_plan_id: str
    capture_plan_digest: str
    capture_radio_ref: str
    capture_radio_id: str
    capture_radio_serial: str
    analysis_plugin_ref: str
    analysis_workers: tuple[AnalysisWorker, ...]
    dashboard_config: Artifact
    ephemeris_config: Artifact
    ephemeris_mode: str
    capacity_config: Artifact
    offhost_config: Artifact
    health_config: Artifact
    units: tuple[UnitArtifact, ...]
    cas_directory_mode: str
    cas_group_name: str
    cas_service_access: Mapping[str, str]
    postgres_host: str
    postgres_port: int
    postgres_database: str
    postgres_tls_mode: str
    credentials: Mapping[str, CredentialBinding]
    dashboard_public_origin: str
    dashboard_proxy_kind: str
    dashboard_auth_policy_ref: str
    dashboard_tls_certificate_ref: str
    dashboard_tls_private_key_ref: str
    ephemeris_receipt_retention_days: int
    health_receipt_retention_days: int
    offhost_receipt_retention_days: int
    health_receipt_path: Path
    incident_receipt_directory: Path
    alert_route_ref: str
    unresolved_fields: tuple[str, ...]
    manifest_digest: str

    @property
    def artifacts(self) -> tuple[tuple[str, Artifact], ...]:
        return (
            ("capture.config", self.capture_config),
            *tuple(
                (f"analysis.{worker.instance}.config", worker.config)
                for worker in self.analysis_workers
            ),
            ("dashboard.config", self.dashboard_config),
            ("ephemeris.config", self.ephemeris_config),
            ("storage.capacity_config", self.capacity_config),
            ("storage.offhost_config", self.offhost_config),
            ("health.config", self.health_config),
            *tuple((f"unit.{unit.unit}", unit.artifact) for unit in self.units),
        )


def load_manifest(path: Path) -> SiteManifest:
    """Load the closed manifest without accessing any referenced candidate."""

    try:
        raw = path.read_bytes()
        if len(raw) > MAX_MANIFEST_BYTES:
            raise SiteReadinessError("site manifest exceeds its size bound")
        document = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SiteReadinessError("site manifest is unreadable") from error
    root = _mapping(document, "manifest")
    _exact(
        root,
        {
            "schema_id",
            "schema_version",
            "site_id",
            "inputs",
            "storage_policy",
            "postgres",
            "dashboard_policy",
            "operations",
        },
        "manifest",
    )
    if root["schema_id"] != SCHEMA_ID or root["schema_version"] != SCHEMA_VERSION:
        raise SiteReadinessError("site manifest schema is unsupported")

    inputs = _mapping(root["inputs"], "inputs")
    _exact(
        inputs,
        {"capture", "analysis", "dashboard", "ephemeris", "storage", "health", "units"},
        "inputs",
    )
    capture = _mapping(inputs["capture"], "inputs.capture")
    _exact(
        capture,
        {
            "config",
            "plan_source_ref",
            "plan_id",
            "plan_digest",
            "radio_ref",
            "radio_id",
            "radio_serial",
        },
        "inputs.capture",
    )
    analysis = _mapping(inputs["analysis"], "inputs.analysis")
    _exact(analysis, {"plugin_ref", "workers"}, "inputs.analysis")
    workers = tuple(
        _worker(value, index)
        for index, value in enumerate(_sequence(analysis["workers"], "workers"))
    )
    if not 1 <= len(workers) <= 64 or len(
        {worker.instance for worker in workers}
    ) != len(workers):
        raise SiteReadinessError("analysis worker instances must be bounded and unique")
    dashboard = _mapping(inputs["dashboard"], "inputs.dashboard")
    _exact(dashboard, {"config"}, "inputs.dashboard")
    ephemeris = _mapping(inputs["ephemeris"], "inputs.ephemeris")
    _exact(ephemeris, {"config", "mode"}, "inputs.ephemeris")
    storage = _mapping(inputs["storage"], "inputs.storage")
    _exact(storage, {"capacity_config", "offhost_config"}, "inputs.storage")
    health = _mapping(inputs["health"], "inputs.health")
    _exact(health, {"config"}, "inputs.health")
    units = tuple(
        _unit_artifact(value, index)
        for index, value in enumerate(_sequence(inputs["units"], "units"))
    )
    if (
        len(units) != len(_REQUIRED_UNITS)
        or {unit.unit for unit in units} != _REQUIRED_UNITS
    ):
        raise SiteReadinessError("site unit inventory must be exact")

    storage_policy = _mapping(root["storage_policy"], "storage_policy")
    _exact(storage_policy, {"cas_acl"}, "storage_policy")
    cas_acl = _mapping(storage_policy["cas_acl"], "storage_policy.cas_acl")
    _exact(cas_acl, {"directory_mode", "group_name", "service_access"}, "cas_acl")
    service_access = _string_mapping(
        cas_acl["service_access"],
        {"capture", "analysis", "dashboard"},
        "cas_acl.service_access",
    )

    postgres = _mapping(root["postgres"], "postgres")
    _exact(postgres, {"endpoint", "credentials"}, "postgres")
    endpoint = _mapping(postgres["endpoint"], "postgres.endpoint")
    _exact(endpoint, {"host", "port", "database", "tls_mode"}, "postgres.endpoint")
    credentials_value = _mapping(postgres["credentials"], "postgres.credentials")
    _exact(credentials_value, set(_ROLES), "postgres.credentials")
    credentials = {role: _credential(credentials_value[role], role) for role in _ROLES}
    if len({item.name for item in credentials.values()}) != len(credentials):
        raise SiteReadinessError("credential names must be distinct")
    if len({item.source_path for item in credentials.values()}) != len(credentials):
        raise SiteReadinessError("credential source paths must be distinct")

    dashboard_policy = _mapping(root["dashboard_policy"], "dashboard_policy")
    _exact(
        dashboard_policy,
        {
            "public_origin",
            "proxy_kind",
            "auth_policy_ref",
            "tls_certificate_ref",
            "tls_private_key_ref",
        },
        "dashboard_policy",
    )
    operations = _mapping(root["operations"], "operations")
    _exact(
        operations,
        {
            "ephemeris_receipt_retention_days",
            "health_receipt_retention_days",
            "offhost_receipt_retention_days",
            "health_receipt_path",
            "incident_receipt_directory",
            "alert_route_ref",
        },
        "operations",
    )
    health_receipt_path = _absolute_path(
        operations["health_receipt_path"], "operations.health_receipt_path"
    )
    incident_directory = _absolute_path(
        operations["incident_receipt_directory"],
        "operations.incident_receipt_directory",
    )
    unresolved = tuple(_placeholder_fields(root))
    digest = "sha256:" + hashlib.sha256(_canonical(root)).hexdigest()
    return SiteManifest(
        site_id=_token(root["site_id"], "site_id"),
        capture_config=_artifact(capture["config"], "inputs.capture.config"),
        capture_plan_source_ref=_token(capture["plan_source_ref"], "plan_source_ref"),
        capture_plan_id=_token(capture["plan_id"], "plan_id"),
        capture_plan_digest=_digest_string(capture["plan_digest"], "plan_digest"),
        capture_radio_ref=_token(capture["radio_ref"], "radio_ref"),
        capture_radio_id=_token(capture["radio_id"], "radio_id"),
        capture_radio_serial=_token(capture["radio_serial"], "radio_serial"),
        analysis_plugin_ref=_plugin_ref(analysis["plugin_ref"]),
        analysis_workers=workers,
        dashboard_config=_artifact(dashboard["config"], "inputs.dashboard.config"),
        ephemeris_config=_artifact(ephemeris["config"], "inputs.ephemeris.config"),
        ephemeris_mode=_string(ephemeris["mode"], "ephemeris.mode"),
        capacity_config=_artifact(storage["capacity_config"], "capacity_config"),
        offhost_config=_artifact(storage["offhost_config"], "offhost_config"),
        health_config=_artifact(health["config"], "health.config"),
        units=units,
        cas_directory_mode=_string(cas_acl["directory_mode"], "directory_mode"),
        cas_group_name=_token(cas_acl["group_name"], "group_name"),
        cas_service_access=service_access,
        postgres_host=_string(endpoint["host"], "postgres.endpoint.host"),
        postgres_port=_bounded_integer(
            endpoint["port"], "postgres.endpoint.port", 1, 65535
        ),
        postgres_database=_token(endpoint["database"], "postgres.endpoint.database"),
        postgres_tls_mode=_string(endpoint["tls_mode"], "postgres.endpoint.tls_mode"),
        credentials=credentials,
        dashboard_public_origin=_string(
            dashboard_policy["public_origin"], "public_origin"
        ),
        dashboard_proxy_kind=_token(dashboard_policy["proxy_kind"], "proxy_kind"),
        dashboard_auth_policy_ref=_token(
            dashboard_policy["auth_policy_ref"], "auth_policy_ref"
        ),
        dashboard_tls_certificate_ref=_token(
            dashboard_policy["tls_certificate_ref"], "tls_certificate_ref"
        ),
        dashboard_tls_private_key_ref=_token(
            dashboard_policy["tls_private_key_ref"], "tls_private_key_ref"
        ),
        ephemeris_receipt_retention_days=_bounded_integer(
            operations["ephemeris_receipt_retention_days"],
            "ephemeris_receipt_retention_days",
            1,
            3650,
        ),
        health_receipt_retention_days=_bounded_integer(
            operations["health_receipt_retention_days"],
            "health_receipt_retention_days",
            1,
            3650,
        ),
        offhost_receipt_retention_days=_bounded_integer(
            operations["offhost_receipt_retention_days"],
            "offhost_receipt_retention_days",
            1,
            3650,
        ),
        health_receipt_path=health_receipt_path,
        incident_receipt_directory=incident_directory,
        alert_route_ref=_token(operations["alert_route_ref"], "alert_route_ref"),
        unresolved_fields=unresolved,
        manifest_digest=digest,
    )


def qualify_manifest(
    manifest: SiteManifest, repository_root: Path
) -> dict[str, object]:
    """Qualify pinned local candidates and return a deterministic no-contact receipt."""

    gates: list[Gate] = []

    def gate(name: str, passed: bool, detail: str) -> None:
        gates.append(Gate(name, passed, detail))

    try:
        root = repository_root.resolve(strict=True)
    except OSError as error:
        raise SiteReadinessError("repository root is unavailable") from error
    if not root.is_dir():
        raise SiteReadinessError("repository root is not a directory")

    artifacts = manifest.artifacts
    gate(
        "manifest.inputs.resolved",
        not manifest.unresolved_fields,
        ",".join(manifest.unresolved_fields) or "complete",
    )
    destinations = [artifact.destination_path for _, artifact in artifacts]
    sources = [artifact.source_path for _, artifact in artifacts]
    gate(
        "manifest.destinations.unique",
        len(set(destinations)) == len(destinations),
        str(len(destinations)),
    )
    gate(
        "manifest.sources.unique",
        len(set(sources)) == len(sources),
        str(len(sources)),
    )

    candidates: dict[Artifact, _CapturedCandidate] = {}
    install_plan: list[dict[str, str]] = []
    for identity, artifact in artifacts:
        placeholders_absent = False
        try:
            candidate = _capture_candidate(root, artifact.source_path)
            placeholders_absent = not any(
                marker in candidate.text.upper() for marker in _PLACEHOLDERS
            )
            observed = "sha256:" + hashlib.sha256(candidate.payload).hexdigest()
            passed = observed == artifact.sha256
            detail = observed
            candidates[artifact] = candidate
        except (OSError, UnicodeError, ValueError, SiteReadinessError):
            passed = False
            detail = "unavailable"
        gate(f"artifact.{identity}.pinned", passed, detail)
        gate(
            f"artifact.{identity}.resolved",
            placeholders_absent,
            "complete" if placeholders_absent else "unresolved",
        )
        install_plan.append(
            {
                "identity": identity,
                "source_path": artifact.source_path,
                "destination_path": str(artifact.destination_path),
                "sha256": artifact.sha256,
            }
        )

    configs: dict[str, object] = {}
    loaders = (
        ("capture", manifest.capture_config, load_service_config),
        *tuple(
            (f"analysis.{worker.instance}", worker.config, load_service_config)
            for worker in manifest.analysis_workers
        ),
        ("dashboard", manifest.dashboard_config, load_service_config),
        ("ephemeris", manifest.ephemeris_config, load_canary_config),
        ("capacity", manifest.capacity_config, load_configuration),
        ("offhost", manifest.offhost_config, load_offhost_config),
        ("health", manifest.health_config, load_health_config),
    )
    for identity, artifact, loader in loaders:
        config_candidate = candidates.get(artifact)
        if config_candidate is None:
            gate(f"config.{identity}.strict", False, "candidate-unavailable")
            continue
        try:
            configs[identity] = _load_captured(config_candidate, loader)
            gate(f"config.{identity}.strict", True, "loaded")
        except Exception:  # noqa: BLE001 - report stable gate detail only.
            gate(f"config.{identity}.strict", False, "rejected")

    _qualify_configs(manifest, configs, gate)
    _qualify_units(manifest, candidates, configs, gate)

    gates.sort(key=lambda item: item.name)
    passed = all(item.passed for item in gates)
    offhost_config = configs.get("offhost")
    cas = getattr(offhost_config, "cas", None)
    capacity_roots = tuple(getattr(configs.get("capacity"), "roots", ()))
    return {
        "schema_id": RECEIPT_SCHEMA_ID,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "site_id": manifest.site_id,
        "manifest_digest": manifest.manifest_digest,
        "status": "pass" if passed else "fail",
        "mode": "offline-no-contact",
        "external_access": {
            "radio": False,
            "network": False,
            "cas_mount": False,
            "postgresql": False,
            "credentials_resolved": False,
            "systemd_manager": False,
        },
        "qualified_inputs": {
            "capture": {
                "plan_source_ref": manifest.capture_plan_source_ref,
                "plan_id": manifest.capture_plan_id,
                "plan_digest": manifest.capture_plan_digest,
                "radio_ref": manifest.capture_radio_ref,
                "radio_id": manifest.capture_radio_id,
                "radio_serial": manifest.capture_radio_serial,
            },
            "analysis": {
                "plugin_ref": manifest.analysis_plugin_ref,
                "workers": [worker.instance for worker in manifest.analysis_workers],
            },
            "ephemeris": {
                "mode": manifest.ephemeris_mode,
                "receipt_retention_days": manifest.ephemeris_receipt_retention_days,
            },
            "storage": {
                "cas_root": str(getattr(cas, "root", "")),
                "mount_source": getattr(cas, "mount_source", ""),
                "filesystem_type": getattr(cas, "filesystem_type", ""),
                "mount_root": getattr(cas, "mount_root", ""),
                "group_name": manifest.cas_group_name,
                "directory_mode": manifest.cas_directory_mode,
                "service_access": dict(manifest.cas_service_access),
                "capacity_roots": sorted(
                    str(getattr(root, "path", "")) for root in capacity_roots
                ),
            },
            "postgres_endpoint": {
                "host": manifest.postgres_host,
                "port": manifest.postgres_port,
                "database": manifest.postgres_database,
                "tls_mode": manifest.postgres_tls_mode,
                "credential_names": {
                    role: manifest.credentials[role].name for role in _ROLES
                },
            },
            "dashboard": {
                "public_origin": manifest.dashboard_public_origin,
                "proxy_kind": manifest.dashboard_proxy_kind,
                "auth_policy_ref": manifest.dashboard_auth_policy_ref,
                "tls_certificate_ref": manifest.dashboard_tls_certificate_ref,
                "tls_private_key_ref": manifest.dashboard_tls_private_key_ref,
            },
            "retention": {
                "ephemeris_receipt_days": manifest.ephemeris_receipt_retention_days,
                "health_receipt_days": manifest.health_receipt_retention_days,
                "offhost_receipt_days": manifest.offhost_receipt_retention_days,
                "health_receipt_path": str(manifest.health_receipt_path),
                "incident_receipt_directory": str(manifest.incident_receipt_directory),
                "alert_route_ref": manifest.alert_route_ref,
            },
        },
        "install_plan": sorted(install_plan, key=lambda item: item["destination_path"]),
        "gates": [asdict(item) for item in gates],
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    parser = argparse.ArgumentParser(prog="leo-flow-site-readiness")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--repository-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        manifest = load_manifest(arguments.manifest)
        receipt = qualify_manifest(manifest, arguments.repository_root)
    except Exception:  # noqa: BLE001 - never expose candidate paths or content.
        errors.write('{"event":"site_readiness_failed"}\n')
        errors.flush()
        return 3
    output.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
    output.flush()
    return 0 if receipt["status"] == "pass" else 2


def _qualify_configs(
    manifest: SiteManifest,
    configs: Mapping[str, object],
    gate: Any,
) -> None:
    capture = configs.get("capture")
    gate(
        "capture.identity",
        getattr(capture, "process", None) == "capture"
        and getattr(capture, "plan_source_ref", None)
        == manifest.capture_plan_source_ref
        and manifest.capture_plan_source_ref == PLAN_SOURCE_REF
        and manifest.capture_plan_id == str(PLAN_ID)
        and manifest.capture_plan_digest == str(SCAN_PLAN_DIGEST)
        and getattr(capture, "radio_ref", None) == manifest.capture_radio_ref
        and manifest.capture_radio_ref == RADIO_REF
        and manifest.capture_radio_id == str(RADIO_ID)
        and manifest.capture_radio_serial == CAPTURE_IDENTITY.radio_serial,
        f"{manifest.capture_plan_id}/{manifest.capture_radio_id}",
    )
    _credential_ref_gate("capture", capture, gate)

    instance_ids: list[str] = []
    for worker in manifest.analysis_workers:
        config = configs.get(f"analysis.{worker.instance}")
        instance_id = getattr(getattr(config, "runtime", None), "instance_id", "")
        instance_ids.append(str(instance_id))
        gate(
            f"analysis.{worker.instance}.identity",
            getattr(config, "process", None) == "analysis" and bool(instance_id),
            str(instance_id) or "missing",
        )
        _credential_ref_gate(f"analysis.{worker.instance}", config, gate)
    gate(
        "analysis.instance_ids.unique",
        len(set(instance_ids)) == len(instance_ids) and all(instance_ids),
        str(len(instance_ids)),
    )

    dashboard = configs.get("dashboard")
    bind_host = getattr(dashboard, "bind_host", None)
    bind_port = getattr(dashboard, "bind_port", None)
    server_ref = getattr(dashboard, "server_ref", None)
    loopback_bind = server_ref == _DASHBOARD_LOOPBACK_SERVER_REF and bind_host in {
        "127.0.0.1",
        "::1",
    }
    explicit_remote_bind = (
        server_ref == _DASHBOARD_REMOTE_SERVER_REF and bind_host == "0.0.0.0"
    )
    gate(
        "dashboard.explicit_bind_policy",
        getattr(dashboard, "process", None) == "dashboard"
        and (loopback_bind or explicit_remote_bind),
        f"{server_ref}@{bind_host}:{bind_port}",
    )
    _credential_ref_gate("dashboard", dashboard, gate)
    origin = urlsplit(manifest.dashboard_public_origin)
    gate(
        "dashboard.proxy_auth_tls",
        origin.scheme == "https"
        and bool(origin.hostname)
        and origin.username is None
        and origin.password is None
        and origin.path in {"", "/"}
        and not origin.query
        and not origin.fragment
        and manifest.dashboard_proxy_kind in {"nginx", "apache", "caddy", "envoy"},
        manifest.dashboard_public_origin,
    )

    ephemeris = configs.get("ephemeris")
    gate(
        "ephemeris.offline_only",
        manifest.ephemeris_mode == "offline"
        and getattr(ephemeris, "network_approved", None) is False
        and getattr(ephemeris, "credential_capabilities", object()) is None,
        manifest.ephemeris_mode,
    )

    offhost = configs.get("offhost")
    capacity = configs.get("capacity")
    cas = getattr(offhost, "cas", None)
    cas_root = getattr(cas, "root", None)
    capacity_roots = tuple(getattr(capacity, "roots", ()))
    gate(
        "storage.cas_identity",
        cas is not None
        and getattr(cas, "group_name", None) == manifest.cas_group_name
        and manifest.cas_directory_mode == "2770"
        and dict(manifest.cas_service_access)
        == {"capture": "rwx", "analysis": "rwx", "dashboard": "none"},
        str(cas_root) if cas_root is not None else "missing",
    )
    gate(
        "storage.capacity_covers_cas",
        cas_root is not None
        and sum(getattr(root, "path", None) == cas_root for root in capacity_roots)
        == 1,
        str(cas_root) if cas_root is not None else "missing",
    )
    if offhost is None:
        gate("offhost.no_contact_preflight", False, "missing")
    elif isinstance(offhost, QualificationConfig):
        try:
            reports = (
                build_preflight_report(offhost, "capture"),
                build_preflight_report(offhost, "analysis"),
            )
            gate(
                "offhost.no_contact_preflight",
                all(report.passed for report in reports),
                "capture,analysis",
            )
        except Exception:  # noqa: BLE001 - stable failure only.
            gate("offhost.no_contact_preflight", False, "rejected")
    offhost_postgres = getattr(offhost, "postgres", None)
    credential_names = getattr(offhost, "credential_names", {})
    gate(
        "postgres.endpoint_identity",
        _HOST.fullmatch(manifest.postgres_host) is not None
        and manifest.postgres_tls_mode in {"verify-ca", "verify-full"}
        and getattr(offhost_postgres, "database_name", None)
        == manifest.postgres_database,
        f"{manifest.postgres_host}:{manifest.postgres_port}/{manifest.postgres_database}",
    )
    gate(
        "postgres.credential_bindings",
        isinstance(credential_names, Mapping)
        and all(
            credential_names.get(role) == manifest.credentials[role].name
            and manifest.credentials[role].source_path.name
            == manifest.credentials[role].name
            for role in _ROLES
        ),
        ",".join(manifest.credentials[role].name for role in _ROLES),
    )
    gate(
        "operations.retention_and_alerting",
        manifest.health_receipt_path != manifest.incident_receipt_directory
        and min(
            manifest.ephemeris_receipt_retention_days,
            manifest.health_receipt_retention_days,
            manifest.offhost_receipt_retention_days,
        )
        > 0,
        manifest.alert_route_ref,
    )


def _qualify_units(
    manifest: SiteManifest,
    candidates: Mapping[Artifact, _CapturedCandidate],
    configs: Mapping[str, object],
    gate: Any,
) -> Mapping[str, str]:
    texts: dict[str, str] = {}
    for unit_artifact in manifest.units:
        expected_destination = Path("/etc/systemd/system") / unit_artifact.unit
        gate(
            f"unit.{unit_artifact.unit}.destination",
            unit_artifact.artifact.destination_path == expected_destination,
            str(unit_artifact.artifact.destination_path),
        )
        candidate = candidates.get(unit_artifact.artifact)
        if candidate is not None:
            texts[unit_artifact.unit] = candidate.text

    destinations = {
        "capture": (
            manifest.capture_config,
            Path("/etc/leo-flow/v5-scan-capture.json"),
        ),
        "dashboard": (manifest.dashboard_config, Path("/etc/leo-flow/dashboard.json")),
        "ephemeris": (
            manifest.ephemeris_config,
            Path("/etc/leo-flow/ephemeris-provider-canary.json"),
        ),
        "capacity": (
            manifest.capacity_config,
            Path("/etc/leo-flow/storage-capacity.json"),
        ),
        "offhost": (
            manifest.offhost_config,
            Path("/etc/leo-flow/offhost-qualification.json"),
        ),
        "health": (manifest.health_config, Path("/etc/leo-flow/systemd-health.json")),
    }
    for name, (artifact, expected) in destinations.items():
        gate(
            f"path.{name}.config",
            artifact.destination_path == expected,
            str(artifact.destination_path),
        )
    for worker in manifest.analysis_workers:
        expected = Path(f"/etc/leo-flow/analysis-{worker.instance}.json")
        gate(
            f"path.analysis.{worker.instance}.config",
            worker.config.destination_path == expected,
            str(worker.config.destination_path),
        )

    unit_checks = {
        "leo-v5-scan.service": str(manifest.capture_config.destination_path),
        "leo-dashboard.service": str(manifest.dashboard_config.destination_path),
        "leo-storage-capacity.service": str(manifest.capacity_config.destination_path),
        "leo-ephemeris-provider-canary.service": str(
            manifest.ephemeris_config.destination_path
        ),
        "leo-flow-health.service": str(manifest.health_config.destination_path),
    }
    for unit, config_path in unit_checks.items():
        arguments = _exec_arguments(texts.get(unit, ""))
        gate(
            f"wiring.{unit}.config",
            arguments is not None
            and _argument_value(arguments, "--config") == config_path,
            config_path,
        )
    analysis_exec = _exec_arguments(texts.get("leo-offline-analysis@.service", ""))
    gate(
        "wiring.analysis.template",
        analysis_exec is not None
        and _argument_value(analysis_exec, "--config")
        == "/etc/leo-flow/analysis-%i.json"
        and _argument_value(analysis_exec, "--plugin") == manifest.analysis_plugin_ref,
        manifest.analysis_plugin_ref,
    )

    runtime_credentials = {
        "leo-v5-scan.service": manifest.credentials["leo_capture"].source_path,
        "leo-offline-analysis@.service": manifest.credentials[
            "leo_analysis"
        ].source_path,
        "leo-dashboard.service": manifest.credentials["leo_dashboard"].source_path,
    }
    for unit, source in runtime_credentials.items():
        values = _directive_values(texts.get(unit, ""), "Service", "LoadCredential")
        expected_credentials: tuple[str, ...] = (f"catalog-dsn:{source}",)
        if unit == "leo-dashboard.service":
            expected_credentials = (
                f"catalog-dsn:{source}",
                "analysis-cas-root:/etc/leo-flow/secrets/dashboard-analysis-cas-root",
            )
        gate(
            f"wiring.{unit}.credential",
            values == expected_credentials,
            source.name,
        )

    cas_root = getattr(getattr(configs.get("offhost"), "cas", None), "root", None)
    for unit in ("leo-v5-scan.service", "leo-offline-analysis@.service"):
        text = texts.get(unit, "")
        gate(
            f"wiring.{unit}.cas",
            cas_root is not None
            and _directive_words(text, "Unit", "RequiresMountsFor") == {str(cas_root)}
            and _directive_words(text, "Service", "ReadWritePaths") == {str(cas_root)}
            and _directive_words(text, "Service", "SupplementaryGroups")
            == {manifest.cas_group_name},
            str(cas_root) if cas_root is not None else "missing",
        )
    dashboard_text = texts.get("leo-dashboard.service", "")
    gate(
        "wiring.leo-dashboard.service.cas",
        cas_root is not None
        and _directive_words(dashboard_text, "Service", "ReadOnlyPaths")
        == {str(cas_root)}
        and not _directive_words(dashboard_text, "Service", "ReadWritePaths"),
        str(cas_root) if cas_root is not None else "missing",
    )

    health_exec = _exec_arguments(texts.get("leo-flow-health.service", ""))
    gate(
        "wiring.health.receipt",
        health_exec is not None
        and _argument_value(health_exec, "--receipt")
        == str(manifest.health_receipt_path),
        str(manifest.health_receipt_path),
    )
    target_wants = (
        _directive_words(texts.get("leo-flow.target", ""), "Unit", "Wants") or set()
    )
    expected_application_units = {
        "leo-v5-scan.service",
        "leo-dashboard.service",
        "leo-storage-capacity.timer",
        "leo-ephemeris-provider-canary.timer",
        "leo-flow-health.timer",
        *(
            f"leo-offline-analysis@{worker.instance}.service"
            for worker in manifest.analysis_workers
        ),
    }
    observed_application_units = {
        name for name in target_wants if name.startswith("leo-")
    }
    gate(
        "wiring.target.workers_and_timers",
        observed_application_units == expected_application_units,
        ",".join(sorted(observed_application_units)),
    )
    return texts


def _credential_ref_gate(identity: str, config: object, gate: Any) -> None:
    refs: object = getattr(getattr(config, "runtime", None), "secret_refs", None)
    expected_names = (
        ("catalog-dsn", "analysis-cas-root")
        if identity == "dashboard"
        else ("catalog-dsn",)
    )
    passed = (
        isinstance(refs, tuple)
        and tuple(getattr(item, "name", None) for item in refs) == expected_names
        and all(
            getattr(item, "provider", None) == "systemd-credential" for item in refs
        )
    )
    gate(f"{identity}.credential_reference", passed, ",".join(expected_names))


def _directive_values(text: str, section: str, key: str) -> tuple[str, ...]:
    current = ""
    values: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            continue
        name, separator, value = line.partition("=")
        if separator and current == section and name.strip() == key:
            values.append(value.strip())
    return tuple(values)


def _directive_words(text: str, section: str, key: str) -> set[str] | None:
    words: list[str] = []
    try:
        for value in _directive_values(text, section, key):
            words.extend(shlex.split(value, posix=True))
    except ValueError:
        return None
    return set(words)


def _exec_arguments(text: str) -> tuple[str, ...] | None:
    values = _directive_values(text, "Service", "ExecStart")
    if len(values) != 1 or not values[0]:
        return None
    try:
        arguments = tuple(shlex.split(values[0], posix=True))
    except ValueError:
        return None
    return arguments or None


def _argument_value(arguments: tuple[str, ...], option: str) -> str | None:
    positions = [index for index, value in enumerate(arguments) if value == option]
    if len(positions) != 1 or positions[0] + 1 >= len(arguments):
        return None
    return arguments[positions[0] + 1]


def _worker(value: object, index: int) -> AnalysisWorker:
    item = _mapping(value, f"workers[{index}]")
    _exact(item, {"instance", "config"}, f"workers[{index}]")
    return AnalysisWorker(
        _token(item["instance"], f"workers[{index}].instance"),
        _artifact(item["config"], f"workers[{index}].config"),
    )


def _unit_artifact(value: object, index: int) -> UnitArtifact:
    item = _mapping(value, f"units[{index}]")
    _exact(
        item, {"unit", "source_path", "destination_path", "sha256"}, f"units[{index}]"
    )
    return UnitArtifact(
        _string(item["unit"], f"units[{index}].unit"),
        _artifact(item, f"units[{index}]", extra_keys={"unit"}),
    )


def _credential(value: object, role: str) -> CredentialBinding:
    item = _mapping(value, f"credentials.{role}")
    _exact(item, {"name", "source_path"}, f"credentials.{role}")
    return CredentialBinding(
        _token(item["name"], f"credentials.{role}.name"),
        _absolute_path(item["source_path"], f"credentials.{role}.source_path"),
    )


def _artifact(
    value: object, label: str, *, extra_keys: set[str] | None = None
) -> Artifact:
    item = _mapping(value, label)
    _exact(
        item,
        {"source_path", "destination_path", "sha256"} | (extra_keys or set()),
        label,
    )
    return Artifact(
        _string(item["source_path"], f"{label}.source_path"),
        _absolute_path(item["destination_path"], f"{label}.destination_path"),
        _string(item["sha256"], f"{label}.sha256"),
    )


def _absolute_path(value: object, label: str) -> Path:
    raw = _string(value, label)
    if not _is_exact_absolute_path(raw):
        raise SiteReadinessError(f"{label} must be an exact absolute non-root path")
    return Path(raw)


def _is_exact_absolute_path(value: str) -> bool:
    if not value.startswith("/") or value == "/" or "\x00" in value:
        return False
    return all(part not in {"", ".", ".."} for part in value[1:].split("/"))


def _plugin_ref(value: object) -> str:
    reference = _string(value, "analysis.plugin_ref")
    if not _PLUGIN_REF.fullmatch(reference):
        raise SiteReadinessError("analysis plugin reference is invalid")
    return reference


def _placeholder_fields(value: object, path: str = "") -> list[str]:
    fields: list[str] = []
    if isinstance(value, dict):
        for key in sorted(value):
            fields.extend(_placeholder_fields(value[key], f"{path}.{key}".lstrip(".")))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            fields.extend(_placeholder_fields(item, f"{path}[{index}]"))
    elif isinstance(value, str) and any(
        marker in value.upper() for marker in _PLACEHOLDERS
    ):
        fields.append(path)
    return fields


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SiteReadinessError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise SiteReadinessError(f"{label} must be an array")
    return value


def _exact(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise SiteReadinessError(f"{label} fields are not exact")


def _string_mapping(value: object, keys: set[str], label: str) -> Mapping[str, str]:
    item = _mapping(value, label)
    _exact(item, keys, label)
    return {key: _string(item[key], f"{label}.{key}") for key in sorted(keys)}


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SiteReadinessError(f"{label} must be a nonempty string")
    return value


def _token(value: object, label: str) -> str:
    token = _string(value, label)
    if not _TOKEN.fullmatch(token):
        raise SiteReadinessError(f"{label} must be a token")
    return token


def _digest_string(value: object, label: str) -> str:
    digest = _string(value, label)
    if not _DIGEST.fullmatch(digest):
        raise SiteReadinessError(f"{label} must be a SHA-256 digest")
    return digest


def _bounded_integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise SiteReadinessError(f"{label} is outside its bound")
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


if __name__ == "__main__":
    raise SystemExit(main())
