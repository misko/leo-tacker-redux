"""Atomic PostgreSQL backup artifacts and verified restore drills.

Credentials are supplied through an explicit libpq service file. They never
appear in command arguments, manifests, or diagnostics. A manifest is written
last and is the sole completion marker for its adjacent custom-format dump.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from leo_flow.contracts.core import canonical_json_bytes

_SCHEMA: Final = "org.leo-flow.postgres-backup/v2"
_ARCHIVE_POLICY: Final = "preserve-owner-and-acl-v1"
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MIGRATION_QUERY: Final = (
    "SELECT name || E'\\t' || sha256 FROM schema_migration ORDER BY name"
)


class BackupError(RuntimeError):
    """A backup is incomplete, malformed, unverifiable, or cannot restore."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], Mapping[str, str]], CommandResult]


@dataclass(frozen=True)
class BackupManifest:
    backup_id: str
    created_utc_ns: int
    dump_file: str
    dump_sha256: str
    dump_byte_count: int
    pg_dump_version: str
    archive_policy: str
    migrations: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not _TOKEN.fullmatch(self.backup_id):
            raise ValueError("backup_id must be a token")
        if self.created_utc_ns < 0 or self.dump_byte_count < 0:
            raise ValueError("backup time and byte count must be non-negative")
        if self.dump_file != f"{self.backup_id}.dump":
            raise ValueError("dump filename must be derived from backup_id")
        if not re.fullmatch(r"[0-9a-f]{64}", self.dump_sha256):
            raise ValueError("dump_sha256 must be lowercase SHA-256")
        if not self.pg_dump_version:
            raise ValueError("pg_dump_version cannot be empty")
        if self.archive_policy != _ARCHIVE_POLICY:
            raise ValueError("archive policy must preserve owners and ACLs")
        if not self.migrations or tuple(sorted(self.migrations)) != self.migrations:
            raise ValueError("migrations must be non-empty and ordered")
        if len({name for name, _ in self.migrations}) != len(self.migrations):
            raise ValueError("migration names must be unique")
        if any(
            not re.fullmatch(r"[0-9a-f]{64}", digest) for _, digest in self.migrations
        ):
            raise ValueError("migration receipts require SHA-256")


def create_backup(
    destination: Path,
    *,
    backup_id: str,
    created_utc_ns: int,
    service_name: str,
    service_file: Path,
    runner: CommandRunner | None = None,
) -> Path:
    """Create one custom dump and publish its canonical manifest last."""

    _validate_connection(service_name, service_file)
    if not _TOKEN.fullmatch(backup_id):
        raise BackupError("backup_id must be a token")
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BackupError("backup destination cannot be created") from error
    if not destination.is_dir():
        raise BackupError("backup destination is not a directory")
    run = runner or _run
    environment = _environment(service_file)
    version = _checked(run, ["pg_dump", "--version"], environment).stdout.strip()
    migrations = _read_migrations(run, service_name, environment)
    dump = destination / f"{backup_id}.dump"
    partial_dump = destination / f".{backup_id}.dump.partial"
    manifest_path = destination / f"{backup_id}.manifest.json"
    partial_manifest = destination / f".{backup_id}.manifest.partial"
    if dump.exists() and not manifest_path.exists():
        # The manifest is the completion marker. This exact-name dump can only
        # be residue from an interrupted publication of the same backup ID.
        dump.unlink()
    if dump.exists() or manifest_path.exists():
        raise BackupError("backup identity already exists")
    try:
        _checked(
            run,
            [
                "pg_dump",
                "--dbname",
                f"service={service_name}",
                "--format=custom",
                "--file",
                str(partial_dump),
            ],
            environment,
        )
        if not partial_dump.is_file():
            raise BackupError("pg_dump did not create its output")
        partial_dump.chmod(0o600)
        if _read_migrations(run, service_name, environment) != migrations:
            raise BackupError("migration receipts changed during backup")
        digest, byte_count = _file_identity(partial_dump)
        manifest = BackupManifest(
            backup_id,
            created_utc_ns,
            dump.name,
            digest,
            byte_count,
            version,
            _ARCHIVE_POLICY,
            migrations,
        )
        _write_new(partial_manifest, _encode(manifest))
        os.replace(partial_dump, dump)
        os.replace(partial_manifest, manifest_path)
        _fsync_directory(destination)
        return manifest_path
    except Exception:
        partial_dump.unlink(missing_ok=True)
        partial_manifest.unlink(missing_ok=True)
        raise


def verify_backup(manifest_path: Path) -> BackupManifest:
    """Verify canonical metadata and every dump byte before use."""

    try:
        raw = manifest_path.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackupError("backup manifest cannot be read") from error
    if canonical_json_bytes(document) != raw:
        raise BackupError("backup manifest is not canonical JSON")
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "backup_id",
        "created_utc_ns",
        "dump_file",
        "dump_sha256",
        "dump_byte_count",
        "pg_dump_version",
        "archive_policy",
        "migrations",
    }:
        raise BackupError("backup manifest fields differ")
    if document["schema"] != _SCHEMA:
        raise BackupError("backup manifest schema is unsupported")
    try:
        migrations = tuple(tuple(item) for item in document["migrations"])
        manifest = BackupManifest(
            document["backup_id"],
            document["created_utc_ns"],
            document["dump_file"],
            document["dump_sha256"],
            document["dump_byte_count"],
            document["pg_dump_version"],
            document["archive_policy"],
            migrations,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise BackupError("backup manifest values are invalid") from error
    dump = manifest_path.parent / manifest.dump_file
    if not dump.is_file():
        raise BackupError("backup dump is missing")
    digest, byte_count = _file_identity(dump)
    if digest != manifest.dump_sha256 or byte_count != manifest.dump_byte_count:
        raise BackupError("backup dump identity differs from manifest")
    return manifest


def restore_backup(
    manifest_path: Path,
    *,
    service_name: str,
    service_file: Path,
    runner: CommandRunner | None = None,
) -> BackupManifest:
    """Atomically restore into an operator-prepared empty database and audit it.

    No ``--clean`` or database creation flag is used. A non-empty/conflicting
    target fails inside the single restore transaction rather than deleting it.
    """

    _validate_connection(service_name, service_file)
    manifest = verify_backup(manifest_path)
    run = runner or _run
    environment = _environment(service_file)
    dump = manifest_path.parent / manifest.dump_file
    _checked(run, ["pg_restore", "--list", str(dump)], environment)
    _checked(
        run,
        [
            "pg_restore",
            "--exit-on-error",
            "--single-transaction",
            "--dbname",
            f"service={service_name}",
            str(dump),
        ],
        environment,
    )
    restored = _read_migrations(run, service_name, environment)
    if restored != manifest.migrations:
        raise BackupError("restored migration receipts differ from backup")
    return manifest


def _validate_connection(service_name: str, service_file: Path) -> None:
    if not _TOKEN.fullmatch(service_name):
        raise BackupError("service_name must be a token")
    try:
        mode = service_file.stat().st_mode & 0o777
    except OSError as error:
        raise BackupError("libpq service file is unavailable") from error
    if not service_file.is_file() or mode & 0o077:
        raise BackupError("libpq service file must be a private regular file")


def _environment(service_file: Path) -> Mapping[str, str]:
    # Preserve ordinary process lookup and locale, but inject only the explicit
    # credential-bearing libpq service file used by this operation.
    result = dict(os.environ)
    result["PGSERVICEFILE"] = str(service_file.resolve())
    result["PGCONNECT_TIMEOUT"] = "10"
    return result


def _read_migrations(
    runner: CommandRunner, service_name: str, environment: Mapping[str, str]
) -> tuple[tuple[str, str], ...]:
    result = _checked(
        runner,
        [
            "psql",
            "--dbname",
            f"service={service_name}",
            "--no-align",
            "--tuples-only",
            "--command",
            _MIGRATION_QUERY,
        ],
        environment,
    )
    rows = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            raise BackupError("database returned malformed migration receipt")
        rows.append((parts[0], parts[1]))
    try:
        return BackupManifest(
            "validation",
            0,
            "validation.dump",
            "0" * 64,
            0,
            "validation",
            _ARCHIVE_POLICY,
            tuple(rows),
        ).migrations
    except ValueError as error:
        raise BackupError("database migration receipts are invalid") from error


def _checked(
    runner: CommandRunner, command: Sequence[str], environment: Mapping[str, str]
) -> CommandResult:
    try:
        result = runner(command, environment)
    except (OSError, subprocess.SubprocessError) as error:
        raise BackupError(f"{command[0]} could not run") from error
    if result.returncode != 0:
        raise BackupError(f"{command[0]} failed")
    return result


def _run(command: Sequence[str], environment: Mapping[str, str]) -> CommandResult:
    completed = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        env=dict(environment),
        timeout=3600,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _file_identity(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
            count += len(block)
    return digest.hexdigest(), count


def _encode(manifest: BackupManifest) -> bytes:
    return canonical_json_bytes(
        {
            "schema": _SCHEMA,
            "backup_id": manifest.backup_id,
            "created_utc_ns": manifest.created_utc_ns,
            "dump_file": manifest.dump_file,
            "dump_sha256": manifest.dump_sha256,
            "dump_byte_count": manifest.dump_byte_count,
            "pg_dump_version": manifest.pg_dump_version,
            "archive_policy": manifest.archive_policy,
            "migrations": [list(item) for item in manifest.migrations],
        }
    )


def _write_new(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
