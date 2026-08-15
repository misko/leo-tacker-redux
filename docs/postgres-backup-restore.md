# PostgreSQL backup and restore drills

The catalog lives on PostgreSQL storage, not NFS. A backup is a PostgreSQL
custom-format dump plus one canonical manifest containing its SHA-256, byte
count, tool version, exact applied-migration receipts, and archive policy. The
manifest is published last and is the only completion marker.

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

The archive retains object ownership and ACLs. This is required because the
security-definer routines are owned by the non-login `leo_routine_owner` role
and the runtime capability grants are part of the security boundary. Do not add
`--no-owner` or `--no-acl` to either side of the drill.

New backups use manifest schema `org.leo-flow.postgres-backup/v2` and bind the
policy `preserve-owner-and-acl-v1`. Verification and restore reject legacy v1
manifests because they do not prove how the archive handled the security
boundary. Re-create an eligible backup with the reviewed tool; do not add the
v2 field to an old manifest.

## Restore drill

Prepare a new empty PostgreSQL database and a separate private service entry.
The restore command deliberately has no `--clean` or database-creation mode: a
non-empty/conflicting target fails atomically rather than deleting it.

Before creating that empty database on a different cluster, pre-provision the
same database-owner role and these global `NOLOGIN` roles:
`leo_capture`, `leo_analysis`, `leo_dashboard`, `leo_maintenance`, and
`leo_routine_owner`. `pg_dump` archives database objects, owners, and grants,
but it does not create cluster-global roles. Do not run the application
migrations in the restore target first; it must remain empty. If any role is
missing or any object conflicts, restoration fails and the single transaction
rolls back. Discard that empty drill target and prepare a fresh one rather than
using `--clean` against it.

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

For an `0018` to `0019` release, create and verify the pre-upgrade backup first,
apply `0019_dwell_request_ingress.sql`, then verify its exact migration receipt,
role/function gates, one authenticated dwell publication, and one route-scoped
lease. If the migration transaction fails, it rolls back: diagnose the cause
and retry only after correction. Never edit a migration that has a receipt.
Ship a new ordered forward migration (for example `0020_...sql`) for a committed
schema defect.

If post-upgrade application validation fails, stop writers, preserve the failed
database and its evidence, and restore the verified pre-upgrade archive into a
separately prepared empty database. Verify migration receipts, catalog row
identities, runtime role ACLs, security-definer owners, and all registered CAS
objects before an operator-controlled endpoint switch. Do not restore over the
failed database and do not treat a schema downgrade as a rollback mechanism.
