from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from leo_flow.maintenance import (
    BackupError,
    create_backup,
    restore_backup,
    verify_backup,
)
from leo_flow.maintenance.postgres_backup import CommandResult

MIGRATIONS = (
    ("0001_first_slice.sql", "1" * 64),
    ("0002_capability_roles.sql", "2" * 64),
)


class Runner:
    def __init__(self, migrations=MIGRATIONS) -> None:
        self.calls: list[tuple[tuple[str, ...], Mapping[str, str]]] = []
        self.migrations = migrations
        self.psql_calls = 0

    def __call__(
        self, command: Sequence[str], environment: Mapping[str, str]
    ) -> CommandResult:
        self.calls.append((tuple(command), environment))
        if command[:2] == ["pg_dump", "--version"]:
            return CommandResult(0, "pg_dump (PostgreSQL) 16.4\n")
        if command[0] == "psql":
            self.psql_calls += 1
            return CommandResult(
                0, "".join(f"{name}\t{digest}\n" for name, digest in self.migrations)
            )
        if command[0] == "pg_dump":
            output = Path(command[command.index("--file") + 1])
            output.write_bytes(b"PGDMP\x01deterministic-fixture")
            return CommandResult(0)
        if command[:2] == ["pg_restore", "--list"]:
            return CommandResult(0, "; archive listing\n")
        if command[0] == "pg_restore":
            return CommandResult(0)
        raise AssertionError(command)


def service_file(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "pg_service.conf"
    path.write_text("[catalog]\nhost=database\n")
    path.chmod(0o600)
    return path


def make_backup(tmp_path: Path, runner: Runner | None = None) -> tuple[Path, Runner]:
    selected = runner or Runner()
    manifest = create_backup(
        tmp_path / "backups",
        backup_id="catalog-20260813T230000Z",
        created_utc_ns=123,
        service_name="catalog",
        service_file=service_file(tmp_path),
        runner=selected,
    )
    return manifest, selected


def test_backup_publishes_manifest_last_without_credentials(tmp_path: Path) -> None:
    manifest_path, runner = make_backup(tmp_path)
    manifest = verify_backup(manifest_path)

    assert manifest.migrations == MIGRATIONS
    assert manifest.archive_policy == "preserve-owner-and-acl-v1"
    assert manifest.dump_byte_count == len(b"PGDMP\x01deterministic-fixture")
    assert (manifest_path.parent / manifest.dump_file).stat().st_mode & 0o777 == 0o600
    assert runner.psql_calls == 2
    assert not list(manifest_path.parent.glob("*.partial"))
    assert all("password" not in " ".join(command) for command, _ in runner.calls)
    assert all(
        environment["PGSERVICEFILE"].endswith("pg_service.conf")
        for _, environment in runner.calls
    )


def test_verify_rejects_noncanonical_manifest_and_changed_dump(tmp_path: Path) -> None:
    manifest_path, _ = make_backup(tmp_path)
    document = json.loads(manifest_path.read_bytes())
    manifest_path.write_text(json.dumps(document, indent=2))
    with pytest.raises(BackupError, match="canonical"):
        verify_backup(manifest_path)

    manifest_path, _ = make_backup(tmp_path / "second")
    dump = manifest_path.with_name("catalog-20260813T230000Z.dump")
    dump.write_bytes(dump.read_bytes() + b"tampered")
    with pytest.raises(BackupError, match="identity"):
        verify_backup(manifest_path)


def test_verify_and_restore_reject_legacy_unbound_archive_policy(
    tmp_path: Path,
) -> None:
    manifest_path, runner = make_backup(tmp_path)
    legacy = json.loads(manifest_path.read_bytes())
    legacy["schema"] = "org.leo-flow.postgres-backup/v1"
    legacy.pop("archive_policy")
    manifest_path.write_text(json.dumps(legacy, sort_keys=True, separators=(",", ":")))

    with pytest.raises(BackupError, match="fields differ"):
        verify_backup(manifest_path)
    with pytest.raises(BackupError, match="fields differ"):
        restore_backup(
            manifest_path,
            service_name="catalog",
            service_file=service_file(tmp_path),
            runner=runner,
        )


def test_restore_verifies_archive_then_restores_atomically_and_audits(
    tmp_path: Path,
) -> None:
    manifest_path, runner = make_backup(tmp_path)
    restore_backup(
        manifest_path,
        service_name="catalog",
        service_file=service_file(tmp_path),
        runner=runner,
    )
    commands = [command for command, _ in runner.calls]
    restore = [command for command in commands if command[0] == "pg_restore"]
    assert restore[0][1] == "--list"
    assert "--single-transaction" in restore[1]
    assert "--clean" not in restore[1]
    assert "--no-owner" not in restore[1]
    assert "--no-acl" not in restore[1]
    dump = next(
        command
        for command in commands
        if command[0] == "pg_dump" and "--file" in command
    )
    assert "--no-owner" not in dump
    assert "--no-acl" not in dump
    assert commands[-1][0] == "psql"


def test_restore_rejects_changed_migration_inventory(tmp_path: Path) -> None:
    manifest_path, _ = make_backup(tmp_path)
    changed = Runner((("0001_first_slice.sql", "f" * 64),))
    with pytest.raises(BackupError, match="migration receipts"):
        restore_backup(
            manifest_path,
            service_name="catalog",
            service_file=service_file(tmp_path),
            runner=changed,
        )


def test_backup_rejects_migration_change_during_dump(tmp_path: Path) -> None:
    class ChangingInventory(Runner):
        def __call__(self, command, environment):
            if command[0] == "psql" and self.psql_calls == 1:
                self.migrations = (("0001_first_slice.sql", "f" * 64),)
            return super().__call__(command, environment)

    destination = tmp_path / "backups"
    with pytest.raises(BackupError, match="changed during backup"):
        create_backup(
            destination,
            backup_id="backup-1",
            created_utc_ns=1,
            service_name="catalog",
            service_file=service_file(tmp_path),
            runner=ChangingInventory(),
        )
    assert list(destination.iterdir()) == []


def test_private_explicit_service_file_is_required(tmp_path: Path) -> None:
    credentials = service_file(tmp_path)
    credentials.chmod(0o644)
    with pytest.raises(BackupError, match="private"):
        create_backup(
            tmp_path / "backups",
            backup_id="backup-1",
            created_utc_ns=1,
            service_name="catalog",
            service_file=credentials,
            runner=Runner(),
        )


def test_failed_dump_leaves_no_published_backup(tmp_path: Path) -> None:
    class FailingRunner(Runner):
        def __call__(self, command, environment):
            if command[0] == "pg_dump" and "--file" in command:
                Path(command[command.index("--file") + 1]).write_bytes(b"partial")
                return CommandResult(1, stderr="secret detail")
            return super().__call__(command, environment)

    destination = tmp_path / "backups"
    with pytest.raises(BackupError, match="pg_dump failed"):
        create_backup(
            destination,
            backup_id="backup-1",
            created_utc_ns=1,
            service_name="catalog",
            service_file=service_file(tmp_path),
            runner=FailingRunner(),
        )
    assert list(destination.iterdir()) == []
