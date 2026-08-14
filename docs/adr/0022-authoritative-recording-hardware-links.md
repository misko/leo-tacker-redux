# 0022: Authoritative recording-to-hardware links

Status: accepted

## Decision

Keep RecordingManifest v0.1 and capture unchanged: capture receives one
pre-authorized hardware snapshot ID and records that ID, but never publishes,
infers, or selects hardware metadata. After recording publication, an
analysis-owned linker reads the exact digest-verified manifest, resolves its
immutable snapshot ID to the catalog's exact ID/SHA-256 pair, and verifies the
snapshot bytes and normalized projection.

The linker rejects station or radio disagreement and rejects every receiver
chain without exactly one assignment covering the recording's full half-open
capture interval. It then appends one content-derived
`recording_hardware_link`. The database has one link per recording and an exact
composite foreign key to `hardware_snapshot`; neither mutable aliases nor a
`latest` query exist. A role-safe catalog rechecks the recording identity in the
same transaction as the insert.

Cross-recording model request construction must obtain hardware references from
the authoritative link. It must not independently resolve the manifest ID.

## Compatibility and migration

Migration 0011 adds the link table and an exact-reference uniqueness constraint
to hardware snapshots. It does not rewrite recording bytes or RecordingManifest
v0.1, and it deliberately does not synthesize links for existing recordings.
Legacy recordings remain published but are ineligible for hardware-dependent
analysis until an explicit linker/backfill verifies and appends their link.
Required-link reads fail closed when no row exists.

This preserves capture/analysis separation and avoids pretending that an ID-only
legacy manifest proves hardware content before authoritative metadata is
available.

## Consequences

- Capture availability is not coupled to the analysis database or hardware
  publication workflow.
- Hardware-dependent models receive exact ID/digest references tied to the
  immutable recording object pair.
- Wiring changes during a recording are rejected instead of being averaged or
  silently assigned to either epoch.
- Republishing or relinking a recording to different hardware is a conflict; a
  corrected scientific artifact requires a new recording identity.

## Verification

Unit tests cover exact resolution, station/radio/receiver/effective-date gates,
idempotence, and fail-closed legacy reads. PostgreSQL tests run under the real
`leo_analysis` role and cover exact foreign keys, authoritative recording
identity, relink rejection, concurrent retries, grants, and unlinked migration
compatibility.
