# ADR 0012: Atomic dataset snapshot persistence

Status: accepted

## Context

ADR 0010 defines one canonical `DatasetSnapshotBundle` as the authoritative
dataset artifact. Durable publication must not turn its members into individual
files or let a database projection become an alternative source of truth.

## Decision

Analysis publishes the canonical bundle as one immutable content-addressed blob.
It then registers the blob reference, snapshot reference, and ordered normalized
member projection in one PostgreSQL transaction. The snapshot row is the only
catalog visibility point. Reusing a snapshot ID, scientific digest, or
idempotency key is accepted only when the complete blob metadata and projection
are identical; every other reuse is a conflict.

The reader selects by the exact `DatasetSnapshotRef`, verifies catalog and blob
metadata, reads the whole canonical object, decodes both scientific digests, and
compares the authoritative bundle with every projected member. A projection
disagreement fails closed. PostgreSQL never reconstructs truth in place of the
bundle.

Member rows preserve each exact `FeatureSetRef`, but their feature-object digest
does not reference `object_blob`. There is not yet an authoritative feature-set
publication catalog, and inserting an externally supplied reference into
`object_blob` would falsely assert that its bytes had been published to this
store. Until that catalog exists, integrity is closed by the feature-membership
digest, the rich snapshot digest, canonical bundle/projection comparison, and
the feature reader's exact-reference verification when model analysis opens a
member. A future feature catalog may add a foreign key through its own migration;
dataset publication must not pre-register or manufacture that ownership.

Blob creation precedes the database transaction because the blob store and
PostgreSQL do not share a transaction manager. A failed catalog transaction can
leave an unreachable content-addressed object for later garbage collection, but
cannot expose a snapshot, member subset, or unregistered object link.

`leo_analysis` receives append/read access. `leo_dashboard` may read the catalog
projection, while `leo_capture` has no dataset access. Runtime roles receive no
`UPDATE`, `DELETE`, or `TRUNCATE` capability. Locked-test authorization remains
outside this persistence adapter; publication does not invent an opening policy.
Catalog reads explicitly run in a read-only PostgreSQL transaction.

## Consequences

- One snapshot creates one bundle object, never per-member control files.
- Exact retries are idempotent and conflicting retries fail closed.
- Truth is queryable as a projection but remains authoritative only in the
  digest-verified bundle.
- Orphan blob reclamation remains the privileged garbage-collection port's job.
