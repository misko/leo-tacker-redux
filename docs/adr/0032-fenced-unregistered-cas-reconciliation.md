# 0032: Fenced reconciliation of unregistered CAS leaves

## Status

Accepted.

## Context

ADR 0028 safely collects cataloged objects, but publication can crash after the
filesystem CAS hard link becomes durable and before `object_blob` registration.
Those bytes are intentionally unreachable from scientific catalog state. They
cannot be reclaimed by retention policy because no catalog row exists.

Walking storage paths in capture, analysis, or dashboard would reintroduce an
implicit filesystem workflow and unsafe path-derived policy. A token checked
before and after unlink is also insufficient: a delayed collector could wake
after its lease, delete a newly published object, and lose its final DB update.

## Decision

Unregistered-object inventory is an explicit maintenance port. Its filesystem
adapter recognizes only `sha256/<lowercase-prefix>/<lowercase-64-hex>` regular
leaves beneath a root pinned by directory file descriptor. It does not follow
root, shard, or leaf symlinks. Unexpected names and types are reported as
`corrupt_name` and are never assigned a digest or deleted. Each page has both a
result limit and a directory-entry scan budget; exceeding the latter fails
closed. The cursor and ordering are deterministic, although each page may
rescan up to the configured budget.

The database distinguishes `live`, other `registered`, `tombstone`, GC or
orphan `in_flight`, and `unregistered`. The first observation time comes from
the PostgreSQL clock. Filesystem mtime, inode, device, size, and pinned shard
identity are exact-change evidence only; none determines retention or
scientific meaning. An orphan must preserve identical evidence for the entire
configured grace period before it can be claimed.

Registration and reconciliation take the same transaction-scoped advisory lock
derived from the full digest identity. Claim state is durable and has no lease:
registration rejects an unresolved claim. A restart resumes the persisted
token, including the crash point after unlink and before completion. Most
importantly, the PostgreSQL adapter holds the digest lock transaction open
while the injected maintenance deleter rechecks the pinned shard and leaf and
unlinks it. Registration therefore commits before claim and defeats deletion,
or waits until deletion commits and then publishes safely. There is no interval
in which a stale authorized worker can delete after registration.

Deletion failures remain claimed because an external failure is ambiguous. The
failure and every observation, evidence change, claim, deletion, and later
registration are append-only audit events. Claim-scoped events use the stable
non-null token for idempotency; repeated null-token events describe distinct
physical incarnations and are intentionally retained. Retrying unlink is idempotent;
absence under the pinned canonical parent completes an earlier interrupted
delete. The command is report-only unless the operator explicitly supplies
`--delete`, which composes the otherwise unavailable deletion adapter.

## Consequences and limits

The inventory is local-filesystem specific; object-store implementations need
their own bounded inventory and exact-delete adapter. A scan proves only
physical/catalog reconciliation, never byte-content validity, ground truth, or
retention intent. The existing content audit remains responsible for hashing
registered bytes. Concurrent uncoordinated privileged mutation inside a shard
is outside the service protocol, but root/shard substitution and symlink
traversal fail closed.
