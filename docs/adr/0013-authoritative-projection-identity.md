# 0013: Authoritative identity for dashboard projection retries

Status: accepted

## Context

ADR 0011 makes dashboard rows append-only and permits corrections. A retry can
reuse a row when its dashboard DTO is unchanged, but DTO equality cannot prove
that the same published artifact produced it. Two feature or model bundles can
legitimately produce identical visible scores or counts. Treating those as one
publication would hide an immutable-identity conflict.

## Decision

Persist a canonical authoritative identity next to each immutable logical
projection identity. This metadata is a rebuildable projection invariant, not a
publication catalog: it contains no artifact lifecycle, locator discovery, or
scientific payload, and it is recreated only from a validated public bundle/ref.

Capture owns recording and activity identities. Analysis owns feature, model,
release, and track identities. Separate tables let PostgreSQL enforce that
capability boundary without row-level policy. The dashboard role has no access.

The stable identities close over:

- recording: manifest digest and the recording object's locator-independent
  identity digest;
- activity: the recording identity and canonical activity manifest;
- feature: feature-set ID, analysis run, and exact published bundle object;
- model: model run and exact published bundle object;
- release: canonical approval and exact model reference;
- track: canonical track DTO, radio ID, and exact model reference.

Object identity fields exclude replaceable locators. Moving verified bytes without
changing digest, size, media type, or format is an idempotent retry, not a new
scientific identity.

A writer takes transaction-scoped locks in sorted logical-key order. It validates
the command and authoritative catalog reference, compares or inserts the identity,
and appends all associated DTO rows in one transaction. An identical retry returns
the existing sequences. A different authoritative identity for the same logical ID
is rejected even when dashboard-visible values are equal. Any failure rolls back
both identity and DTO rows.

Storage health has no immutable artifact identity. Identical consecutive
observations are idempotent; changed observations append normally.

## Consequences

- Projection retries are scientifically honest and concurrently safe.
- Capture always creates a neutral `pending` recording projection and has no API
  for asserting analysis state. Feature projection atomically appends the
  `complete` correction with its feature rows. Capture retries preserve it.
- Recording-state corrections may append under the same authoritative recording
  identity; immutable activity, feature, model, release, and track IDs cannot be
  rebound.
- The identity tables can be dropped and rebuilt from authoritative artifacts.
- PostgreSQL fault, role, retry, and same-DTO/different-authority tests enforce the
  boundary.
