# 0011: PostgreSQL dashboard projections

Status: accepted

## Context

The immutable recording catalog identifies authoritative published objects but
intentionally does not duplicate manifest, feature, model, or track content.
Dashboard queries need bounded, indexed access to those facts without opening
object blobs or scanning storage directories.

## Decision

Use normalized, append-only PostgreSQL projection tables for dashboard-facing
recording, activity, feature, model, track, and storage-health DTOs. These are
rebuildable read models, not authoritative artifacts or another publication
catalog. Recording projections reference the authoritative recording catalog.

Projection writers remain capture/analysis capabilities; `leo_dashboard` receives
table and projection-sequence `SELECT` only, without sequence `USAGE`. The dashboard
query adapter has no mutation API, starts every transaction read-only, and verifies
that transaction state before issuing a projection query.

All projection tables draw sequence values from one global sequence. Keyset cursors
carry the query fingerprint, last sort key, and global high-water mark. Later pages
therefore retain the first page's logical snapshot even when projection writers
append rows concurrently. Page size is bounded in the adapter.

The adapter issues only static, parameterized SQL and never constructs storage
paths or reads recording objects.

## Consequences

- Projection writers append a complete DTO only after the corresponding
  authoritative artifact is published.
- Corrections append a newer row for the same logical identity; readers select the
  latest row at or below the cursor high-water mark.
- Storage health is an explicitly published observation. Absence means unavailable;
  the dashboard does not probe a filesystem.
- A later retention policy must preserve rows needed by outstanding cursors or
  explicitly version and expire cursors.
- Migration, privilege, query, injection, and concurrent-pagination tests enforce
  this decision.
