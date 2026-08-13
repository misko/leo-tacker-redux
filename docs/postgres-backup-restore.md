# PostgreSQL backup and restore drills

The catalog lives on PostgreSQL storage, not NFS. A backup is a PostgreSQL
custom-format dump plus one canonical manifest containing its SHA-256, byte
count, tool version, and exact applied-migration receipts. The manifest is
published last and is the only completion marker.

Credentials belong in an explicitly selected private libpq service file:

```ini
[leo-catalog-backup]
host=kalman
port=5432
dbname=leo_flow
user=leo_backup
passfile=/run/credentials/leo-catalog.pgpass
```

Both files must be readable only by the service account. The service name—not
the DSN or password—appears in the process arguments.

```bash
python -m leo_flow.maintenance backup \
  --destination /mnt/qnap/leo-flow/catalog-backups \
  --backup-id catalog-20260813T230000Z \
  --service leo-catalog-backup \
  --service-file /run/credentials/leo-catalog-service.conf

python -m leo_flow.maintenance verify-backup \
  --manifest /mnt/qnap/leo-flow/catalog-backups/catalog-20260813T230000Z.manifest.json
```

`backup` reads migration receipts both before and after `pg_dump`; a concurrent
schema change invalidates the attempt. Failed attempts leave no manifest and
remove their temporary files; if interruption occurs between the two final
renames, an exact-name dump without a manifest is treated as incomplete and
replaced on retry. The tool never emits command stderr because it may contain
deployment details.

## Restore drill

Prepare a new empty PostgreSQL database and a separate private service entry.
The restore command deliberately has no `--clean` or database-creation mode: a
non-empty/conflicting target fails atomically rather than deleting it.

```bash
python -m leo_flow.maintenance restore-drill \
  --manifest /mnt/qnap/leo-flow/catalog-backups/catalog-20260813T230000Z.manifest.json \
  --service leo-catalog-restore-drill \
  --service-file /run/credentials/leo-catalog-restore-service.conf
```

The command verifies every dump byte, validates the archive, restores it in one
transaction, and compares restored migration receipts with the manifest. Finish
the drill by reading and hashing every object registered in the restored catalog:

```bash
python -m leo_flow.maintenance audit-objects \
  --blob-root /mnt/qnap/leo-flow/cas \
  --service leo-catalog-restore-drill \
  --service-file /run/credentials/leo-catalog-restore-service.conf
```

Database restoration alone cannot prove bytes held by the blob store. The audit
uses the catalog only as an inventory and asks the CAS adapter to verify each
exact byte count and SHA-256. It reports all missing/corrupt objects without
including storage exception details and exits nonzero if any object fails.
