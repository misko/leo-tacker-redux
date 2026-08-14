"""Operator CLI for explicit PostgreSQL backup and restore drills."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .object_gc import GarbageCollectionError
from .orphan_reconciliation import OrphanReconciliationError
from .postgres_backup import BackupError, create_backup, restore_backup, verify_backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="leo-flow-maintenance")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--destination", required=True, type=Path)
    backup.add_argument("--backup-id", required=True)
    _connection_arguments(backup)
    verify = commands.add_parser("verify-backup")
    verify.add_argument("--manifest", required=True, type=Path)
    restore = commands.add_parser("restore-drill")
    restore.add_argument("--manifest", required=True, type=Path)
    _connection_arguments(restore)
    audit = commands.add_parser("audit-objects")
    audit.add_argument("--blob-root", required=True, type=Path)
    _connection_arguments(audit)
    gc = commands.add_parser("gc-objects")
    gc.add_argument("--blob-root", required=True, type=Path)
    gc.add_argument("--limit", type=int, default=100)
    gc.add_argument("--claim-ttl-seconds", type=int, default=300)
    _connection_arguments(gc)
    orphan = commands.add_parser("reconcile-orphans")
    orphan.add_argument("--blob-root", required=True, type=Path)
    orphan.add_argument("--after")
    orphan.add_argument("--limit", type=int, default=100)
    orphan.add_argument("--scan-budget", type=int, default=100_000)
    orphan.add_argument("--minimum-age-seconds", type=int, default=86_400)
    orphan.add_argument(
        "--delete",
        action="store_true",
        help="enable fenced deletion; default is report-only",
    )
    _connection_arguments(orphan)
    args = parser.parse_args(argv)
    payload: dict[str, object]
    try:
        if args.command == "backup":
            manifest_path = create_backup(
                args.destination,
                backup_id=args.backup_id,
                created_utc_ns=time.time_ns(),
                service_name=args.service,
                service_file=args.service_file,
            )
            payload = {"event": "backup_complete", "manifest": str(manifest_path)}
        elif args.command == "verify-backup":
            manifest = verify_backup(args.manifest)
            payload = {"event": "backup_verified", "backup_id": manifest.backup_id}
        elif args.command == "restore-drill":
            manifest = restore_backup(
                args.manifest,
                service_name=args.service,
                service_file=args.service_file,
            )
            payload = {"event": "restore_verified", "backup_id": manifest.backup_id}
        elif args.command == "audit-objects":
            from leo_flow.maintenance.object_audit import audit_objects
            from leo_flow.maintenance.postgres_objects import (
                PostgresObjectInventory,
                service_connection_factory,
            )
            from leo_flow.storage import FileSystemBlobStore

            report = audit_objects(
                PostgresObjectInventory(
                    service_connection_factory(args.service, args.service_file)
                ),
                FileSystemBlobStore(args.blob_root),
            )
            payload = {
                "event": "object_audit_complete",
                "object_count": report.object_count,
                "verified_count": report.verified_count,
                "failures": [
                    {
                        "object_digest": failure.object_digest,
                        "reason": failure.reason,
                    }
                    for failure in report.failures
                ],
            }
            if not report.passed:
                sys.stderr.write(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
                )
                return 4
        elif args.command == "gc-objects":
            from leo_flow.maintenance.filesystem_gc import (
                MaintenanceFileSystemBlobDeleter,
            )
            from leo_flow.maintenance.object_gc import collect_objects
            from leo_flow.maintenance.postgres_gc import (
                PostgresGarbageCollectionCatalog,
            )
            from leo_flow.maintenance.postgres_objects import (
                service_connection_factory,
            )

            results = collect_objects(
                PostgresGarbageCollectionCatalog(
                    service_connection_factory(args.service, args.service_file)
                ),
                MaintenanceFileSystemBlobDeleter(args.blob_root),
                limit=args.limit,
                claim_ttl_seconds=args.claim_ttl_seconds,
            )
            payload = {
                "event": "object_gc_complete",
                "results": [
                    {
                        "object_digest": result.object_digest,
                        "outcome": result.outcome,
                    }
                    for result in results
                ],
            }
        else:
            try:
                from leo_flow.maintenance.filesystem_orphans import (
                    FileSystemCasInventory,
                    MaintenanceOrphanFileDeleter,
                )
                from leo_flow.maintenance.orphan_reconciliation import (
                    reconcile_unregistered_objects,
                )
                from leo_flow.maintenance.postgres_objects import (
                    service_connection_factory,
                )
                from leo_flow.maintenance.postgres_orphans import (
                    PostgresOrphanReconciliationCatalog,
                )

                with FileSystemCasInventory(
                    args.blob_root, scan_budget=args.scan_budget
                ) as inventory:
                    orphan_report = reconcile_unregistered_objects(
                        inventory,
                        PostgresOrphanReconciliationCatalog(
                            service_connection_factory(args.service, args.service_file)
                        ),
                        after=args.after,
                        limit=args.limit,
                        minimum_age_seconds=args.minimum_age_seconds,
                        deleter=(
                            MaintenanceOrphanFileDeleter(inventory)
                            if args.delete
                            else None
                        ),
                    )
            except Exception as error:
                raise OrphanReconciliationError from error
            payload = {
                "event": "orphan_reconciliation_complete",
                "report_only": orphan_report.report_only,
                "next_cursor": orphan_report.next_cursor,
                "results": [
                    {
                        "key": result.key,
                        "category": result.category.value,
                        "outcome": result.outcome,
                    }
                    for result in orphan_report.results
                ],
            }
    except (BackupError, GarbageCollectionError, OrphanReconciliationError):
        sys.stderr.write('{"event":"maintenance_failed"}\n')
        return 3
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


def _connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--service", required=True)
    parser.add_argument("--service-file", required=True, type=Path)


if __name__ == "__main__":
    raise SystemExit(main())
