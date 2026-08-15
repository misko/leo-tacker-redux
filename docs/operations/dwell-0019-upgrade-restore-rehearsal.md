# Dwell ingress `0019` upgrade and restore rehearsal

On 2026-08-14, the automated qualification test rehearsed the release entirely
off-host in a disposable `postgres:16-alpine` instance and a temporary local CAS
directory. It did not contact a radio, `.15`, NFS, systemd, or a deployment
endpoint.

The test created a catalog at migration `0018`, published one recording and its
exact FeatureSet/CAS object, then applied only
`0019_dwell_request_ingress.sql`. Analysis published one authenticated dwell
request and capture leased it. The production `create_backup` API created a v2
PostgreSQL custom-format archive bound to `preserve-owner-and-acl-v1`; the
production `restore_backup` API restored it transactionally into a separate
empty database. The databases were compared using an exact aggregate row
fingerprint.

The restored database retained `leo_routine_owner` ownership, analysis/capture
function execution grants, denial of direct dwell-table access, all migration
receipts, the recording/FeatureSet/dwell/job identities, and access to the exact
FeatureSet CAS bytes. After the copied lease expired, a restarted capture queue
claimed attempt 2 / lease generation 2 and rejected completion with the stale
pre-backup lease.

The receipt from that run was:

| Evidence | Exact value |
|---|---|
| PostgreSQL major | `16` |
| Migration transition | `0018_tracking_model_snapshot_catalog.sql` → `0019_dwell_request_ingress.sql` |
| Archive policy | `preserve-owner-and-acl-v1` |
| Dump bytes | `278352` |
| Dump SHA-256 | `76f241d4981cf9ccdd6d7a14f09a1e2bbca9bb0b64161290efc6eed4721cddca` |
| Database row fingerprint SHA-256 | `09a4087d70c952f38f21b2b6ed4385730908680ee3473e128014b14ca4d3ccec` |
| Dwell request digest | `sha256:f5baf9066dc97f453359b880988daf0f214781f12e6bcf2f14cb666c0c1f76a4` |
| Restored claim | attempt `2`, lease generation `2` |

Reproduce it with:

```console
uv run pytest -q -s tests/postgres/test_dwell_upgrade_restore.py
```

Each archive is expected to have a different byte identity; retain the receipt
from the exact candidate used for an operator decision. Passing this disposable
rehearsal qualifies the migration and recovery mechanics only. It does not
authorize a live cutover or replace site-specific preflight evidence.
