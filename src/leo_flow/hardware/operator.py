"""Deterministic operator workflow for authoritative hardware snapshots."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Protocol, cast

from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.contracts.core import Digest
from leo_flow.contracts.hardware import (
    HardwareMetadataSnapshot,
    HardwareMetadataSnapshotRef,
)

from .codec import (
    MAX_HARDWARE_SNAPSHOT_BYTES,
    decode_hardware_snapshot,
    encode_hardware_snapshot,
)

OPERATOR_CONFIG_SCHEMA = "org.leo-flow.hardware-operator-config"
OPERATOR_CONFIG_VERSION = "0.1"


class HardwareOperatorError(RuntimeError):
    """An operator input or publication operation failed closed."""


@dataclass(frozen=True)
class HardwarePublicationConfig:
    cas_root: Path
    dsn_credential_name: str

    def __post_init__(self) -> None:
        if not self.cas_root.is_absolute() or self.cas_root == Path("/"):
            raise HardwareOperatorError("CAS root must be a bounded absolute path")
        if (
            not self.dsn_credential_name
            or self.dsn_credential_name in {".", ".."}
            or "/" in self.dsn_credential_name
            or "\x00" in self.dsn_credential_name
        ):
            raise HardwareOperatorError("DSN credential name is invalid")


@dataclass(frozen=True)
class HardwareOperatorConfig:
    snapshot: HardwareMetadataSnapshot
    publication: HardwarePublicationConfig


@dataclass(frozen=True)
class HardwareBundleIdentity:
    ref: HardwareMetadataSnapshotRef
    byte_count: int


class HardwareRepository(Protocol):
    def publish(
        self, snapshot: HardwareMetadataSnapshot, *, idempotency_key: str
    ) -> HardwareMetadataSnapshotRef: ...

    def get(self, ref: HardwareMetadataSnapshotRef) -> HardwareMetadataSnapshot: ...


def load_operator_config(path: Path) -> HardwareOperatorConfig:
    """Load a strict authoring/publication config without resolving its secret."""

    try:
        document = json.loads(path.read_bytes(), object_pairs_hook=_unique)
        root = _object(document, "configuration")
        _keys(root, {"schema", "version", "snapshot", "publication"}, "configuration")
        if (
            root["schema"] != OPERATOR_CONFIG_SCHEMA
            or root["version"] != OPERATOR_CONFIG_VERSION
        ):
            _bad("operator configuration schema is unsupported")

        publication = _object(root["publication"], "publication")
        _keys(publication, {"cas_root", "database_dsn"}, "publication")
        credential = _object(publication["database_dsn"], "database_dsn")
        _keys(credential, {"provider", "name"}, "database_dsn")
        if credential["provider"] != "systemd-credential":
            _bad("database DSN provider must be systemd-credential")

        snapshot = decode_hardware_snapshot(_canonical(root["snapshot"]))
        return HardwareOperatorConfig(
            snapshot,
            HardwarePublicationConfig(
                Path(_string(publication["cas_root"], "cas_root")),
                _string(credential["name"], "database_dsn.name"),
            ),
        )
    except HardwareOperatorError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise HardwareOperatorError(
            "hardware operator configuration is invalid"
        ) from error


def create_bundle(
    config: HardwareOperatorConfig, destination: Path
) -> HardwareBundleIdentity:
    """Create, or idempotently confirm, one exact canonical bundle."""

    payload = encode_hardware_snapshot(config.snapshot)
    identity = _identity(config.snapshot, payload)
    _publish_file(destination, payload)
    return identity


def validate_bundle(
    path: Path, *, expected: HardwareMetadataSnapshot | None = None
) -> tuple[HardwareMetadataSnapshot, HardwareBundleIdentity]:
    """Validate canonical bytes and optionally their exact authored snapshot."""

    payload = _read_file(path)
    try:
        snapshot = decode_hardware_snapshot(payload)
    except ValueError as error:
        raise HardwareOperatorError("hardware bundle is invalid") from error
    if expected is not None and snapshot != expected:
        raise HardwareOperatorError(
            "hardware bundle differs from operator configuration"
        )
    return snapshot, _identity(snapshot, payload)


def publish_bundle(
    config: HardwareOperatorConfig,
    bundle: Path,
    *,
    dry_run: bool,
    repository: HardwareRepository | None = None,
) -> HardwareBundleIdentity:
    """Publish the exact configured bytes, or validate without external I/O."""

    snapshot, identity = validate_bundle(bundle, expected=config.snapshot)
    if dry_run:
        return identity

    selected = repository or _build_repository(config.publication)
    key = hardware_publication_key(identity.ref)
    try:
        published = selected.publish(snapshot, idempotency_key=key)
        if published != identity.ref or selected.get(identity.ref) != snapshot:
            raise HardwareOperatorError(
                "published hardware snapshot failed exact verification"
            )
    except HardwareOperatorError:
        raise
    except Exception as error:
        raise HardwareOperatorError("hardware snapshot publication failed") from error
    return identity


def hardware_publication_key(ref: HardwareMetadataSnapshotRef) -> str:
    """Return the content-bound key; operators do not choose mutable aliases."""

    return f"hardware-metadata:{ref.snapshot_id}:{ref.digest}"


def _build_repository(config: HardwarePublicationConfig) -> HardwareRepository:
    # Heavy/server-only imports and external adapter construction occur only for
    # a real publish. Create, validate, and dry-run stay stdlib-only.
    try:
        from leo_flow.adapters.hardware_postgres_catalog import (
            PostgresHardwareSnapshotCatalog,
            connection_factory,
        )
        from leo_flow.storage.filesystem import FileSystemBlobStore

        from .persistence import DurableHardwareMetadataRepository

        dsn = SystemdCredentialProvider().resolve(config.dsn_credential_name)
        return DurableHardwareMetadataRepository(
            FileSystemBlobStore(config.cas_root),
            PostgresHardwareSnapshotCatalog(connection_factory(dsn)),
        )
    except Exception as error:
        raise HardwareOperatorError(
            "hardware publication adapters cannot be built"
        ) from error


def _identity(
    snapshot: HardwareMetadataSnapshot, payload: bytes
) -> HardwareBundleIdentity:
    return HardwareBundleIdentity(
        HardwareMetadataSnapshotRef(snapshot.snapshot_id, Digest.sha256(payload)),
        len(payload),
    )


def _read_file(path: Path) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        )
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise HardwareOperatorError("hardware bundle is not a regular file")
        if details.st_size > MAX_HARDWARE_SNAPSHOT_BYTES:
            raise HardwareOperatorError("hardware bundle exceeds the size limit")
        payload = bytearray()
        while len(payload) <= MAX_HARDWARE_SNAPSHOT_BYTES:
            chunk = os.read(
                descriptor,
                min(65_536, MAX_HARDWARE_SNAPSHOT_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
    except HardwareOperatorError:
        raise
    except OSError as error:
        raise HardwareOperatorError("hardware bundle cannot be read") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(payload) > MAX_HARDWARE_SNAPSHOT_BYTES:
        raise HardwareOperatorError("hardware bundle exceeds the size limit")
    return bytes(payload)


def _publish_file(path: Path, payload: bytes) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o644,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        _fsync_directory(path.parent)
    except FileExistsError:
        if _read_file(path) != payload:
            raise HardwareOperatorError("bundle path already contains different bytes")
    except HardwareOperatorError:
        raise
    except OSError as error:
        raise HardwareOperatorError("hardware bundle cannot be created") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise HardwareOperatorError("snapshot cannot be encoded canonically") from error


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _bad(f"duplicate configuration key: {key}")
        result[key] = value
    return result


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _bad(f"{label} must be an object")
    return cast(dict[str, object], value)


def _keys(value: dict[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        _bad(f"{label} fields differ from the schema")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        _bad(f"{label} must be a non-empty string")
    return value


def _bad(message: str) -> NoReturn:
    raise HardwareOperatorError(message)
