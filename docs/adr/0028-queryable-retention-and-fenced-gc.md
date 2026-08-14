# 0028: Queryable retention and fenced garbage collection

## Status

Accepted.

## Decision

Retention is explicit catalog data. Each object may have one or more immutable
`object_retention_assignment` rows. An object is a candidate only when it has
at least one policy, every assigned policy permits remote deletion, every
retention-plus-grace deadline has passed, and the exhaustive live-reference
view contains no row for it. A policy that does not permit deletion is an
indefinite hold; path names, formats, and locators never imply retention.

Only `leo_maintenance` may create policies/assignments or execute the three GC
state-transition functions. It has no direct `DELETE` privilege on
`object_blob`. Capture, analysis, and dashboard cannot claim objects or delete
catalog rows. Remote deletion is available only through the separately
injected `MaintenanceBlobDeleter` port.

Claiming locks the `object_blob` row, rechecks every live reference and changes
the lifecycle to `gc_claimed`. Every direct object-FK consumer has a trigger
that takes a conflicting key-share lock and rejects references unless the row
is `live`. Thus either publication commits first and the claim observes its
reference, or the claim commits first and publication fails closed.

After successful byte deletion the catalog row becomes a permanent
`gc_deleted` tombstone. It is deliberately not deleted: an uploader racing a
collector cannot revive the same digest through `INSERT ... ON CONFLICT` and
then publish a live reference to bytes that were just removed. A crash may
leave a claimed row, and an external delete may leave unregistered bytes, but
neither case exposes a live catalog reference to missing bytes. Claims expire
and can be reclaimed; tokens fence stale finalizers. Attempts and sanitized
failures are append-only audit rows.

## Compatibility and extension rule

Migration 0013 enumerates every direct `object_blob` foreign key present
through migration 0012. Any migration adding another object consumer must add
that reference to `object_blob_live_reference` and install
`object_blob_assert_live_reference` on its object columns in the same
transaction. The schema-introspection test intentionally fails when a new FK
is added without updating the boundary.

Normal publishers call `register_live_object_blob` after their blob writer has
published exact bytes. It is the sole resurrection path: a completed tombstone
may become live only when all immutable metadata is identical, and the new
scientific reference is added in that same transaction. Claimed or failed
objects remain fenced. All publisher verification queries also require
`lifecycle_state = 'live'`; generic conflict handling cannot accept a
tombstone. The ordinary roles still have no direct lifecycle-update privilege.
