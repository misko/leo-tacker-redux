# Tracking-input catalog migration and operations

## Migration 0016 lock requirement

Migration 0016 validates and adds five exact authority `UNIQUE` constraints on
`dataset_snapshot`, `dataset_member`, `feature_set`,
`recording_hardware_link`, and `recording_ephemeris_link`. PostgreSQL implements
these transactional `ALTER TABLE ... ADD CONSTRAINT ... UNIQUE` statements with
blocking table locks. The repository migration runner executes each migration
inside one transaction, so `CREATE UNIQUE INDEX CONCURRENTLY` is not a safe
substitute in this migration.

Apply 0016 only in a declared maintenance window:

1. Stop analysis writers to dataset snapshots, feature sets, hardware links,
   and ephemeris links. Capture and dashboard readers need not be stopped, but
   no long-lived transaction may hold conflicting locks.
2. Check `pg_stat_activity` for old transactions and wait or terminate them by
   the normal operator procedure. Do not let the migration wait invisibly
   behind an unbounded session.
3. Take the normal catalog backup and record the current migration receipt.
4. Apply the ordered migrations once. Do not rewrite an already-receipted 0016.
5. Verify the five authority constraints, the tracking-input role privileges,
   and the `tracking_input_snapshot.bundle` live-reference inventory before
   restarting writers.

On 2026-08-13, PostgreSQL 16 (`postgres:16-alpine`) in local Docker applied the
complete 0016 transaction in 0.219 seconds with 10,000 existing rows in each of
`dataset_member`, `feature_set`, `recording_hardware_link`, and
`recording_ephemeris_link`, plus their valid parent authorities. This is a
development measurement, not an availability guarantee; production time is
storage-, contention-, and cardinality-dependent, and the maintenance window
remains mandatory.

## Locator behavior

Tracking jobs and scientific identities exclude locators. Exact lookup accepts
a locator-independent identity and returns the catalog's current full object
reference. Publication still requires the supplied locator to match the live
`object_blob` row exactly.

Operational CAS relocation is not implemented. Existing audit and maintenance
ports can inventory, verify, reconcile, claim, and delete objects, but cannot
atomically move verified bytes and fence a catalog locator switch. The
filesystem CAS also rejects a locator that is not derived from its digest. A
future relocation operation must verify destination bytes, hold the same digest
fences used by publication and GC, atomically update catalog metadata, and make
source retirement retry-safe. Until that complete protocol exists, locator
disagreement is an intentional collision rather than a partial move.
