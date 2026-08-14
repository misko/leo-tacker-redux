# Tracking model snapshot catalog

`tracking_model_snapshot` is the authoritative visibility boundary for joint
tracking-model output. It is intentionally separate from the descriptive
`model_snapshot` and `model_release` catalog.

| Phase | Durable effect | Failure behavior |
|---|---|---|
| validate lease output | Decode the tracking job request and require exact input, config, and algorithm references | Reject before any CAS write |
| write CAS | Write canonical `tracking-model-snapshot-bundle-v0.1` bytes by SHA-256 | No PostgreSQL visibility; an interrupted upload is reconciled as an unregistered object |
| lock lease | Fence token, generation, job type, and expiry | Stale worker cannot register or publish |
| register and publish | In one transaction, register exact CAS metadata and invoke `publish_tracking_model_snapshot` | Any conflict or later completion fault rolls back both object registration and catalog row |
| complete job | Store the exact result artifact on the fenced lease | Output visibility and successful completion commit together |

The definer routine locks and checks the registered live object, including its
digest, byte count, media type, format, and locator. It also locks the complete
locator-free `TrackingInputSnapshotIdentity` against
`tracking_input_snapshot`. The table repeats that identity in a composite
foreign key, so input substitution is rejected both procedurally and
declaratively.

`model_run_id` is the row identity. `model_snapshot_id` is indexed but is not
unique: two provenance-bearing runs may reproduce the same scientific snapshot.
The full output digest, CAS digest, evidence digest, provenance digest, bounded
summary counts, and exact tracking-input identity are stored as queryable
projections. The full canonical output remains in CAS and every repository read
rechecks metadata, bytes, canonical decoding, and all projections.

## Capabilities and retention

| Role | Catalog | Publisher |
|---|---|---|
| `leo_analysis` | `SELECT` only | execute |
| `leo_dashboard` | `SELECT` only | denied |
| `leo_capture` | denied | denied |
| `leo_maintenance` | denied | denied |
| `leo_routine_owner` | narrowly granted for definer execution; `NOLOGIN` | owner |

The routine has `SECURITY DEFINER` with `search_path = pg_catalog, pg_temp` and
uses fully qualified relations. The output bundle is included in
`object_blob_live_reference`; normal garbage collection cannot claim it while
the catalog row exists. There is no ordinary delete path for model snapshots.

Migration `0018_tracking_model_snapshot_catalog.sql` is append-only and works in
the normal fresh migration sequence after security hardening migration `0017`.
Before upgrade, confirm that no site-local relation or routine already uses the
new names. After upgrade, verify the routine owner/search path/execute ACL,
table role matrix, live-reference inventory, and a publication/read round trip.
Rollback is restore-based after publication because removing an authoritative
append-only catalog would discard scientific visibility.
