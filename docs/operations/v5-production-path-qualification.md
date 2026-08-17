# V5 composed production-path qualification

`leo_flow.deployments.v5_production_path_e2e` is an explicitly armed,
single-request qualification of the deployed cross-component boundary:

```text
qualified scan recording
  -> PostgreSQL recording + recording-analysis job + FeatureSet
  -> authenticated PostgreSQL DwellRequest
  -> route-scoped lease + heartbeat
  -> exclusive receive-only V5 supervisor
  -> PostgreSQL recording + recording-analysis job + FeatureSet
```

It is a bounded qualification harness, not a daemon and not a second workflow
engine. Capture consumes only a public `DwellRequest`, produces a public
recording, and has no analysis dependency. Analysis runs only after the local
capture pair has been atomically published to the catalog and removed from the
capture spool.

## Required inputs

| Input | Requirement |
|---|---|
| Radio | Exact V5 serial `104000b29905000e17000800065934759d` at `ip:192.168.1.15` |
| Clock | Operator verifies host NTP immediately before execution and supplies the exact confirmation `host-ntp-synchronized` |
| Source | Local CAS from the immutable 2026-08-14 qualified scan; both source objects are re-hashed through the public blob-reader port |
| Output | New or empty absolute directory on an approved local filesystem: `ext2`, `ext3`, `ext4`, `xfs`, `btrfs`, `f2fs`, `bcachefs`, `zfs`, `tmpfs`, or `overlay` |
| Database | Approved disposable PostgreSQL 16 database and owner on one exact pinned cluster system identifier; every application table must be empty |
| Migrations | Private reviewed directory whose 33 exact file hashes match all database receipts through `0033_registered_analysis_during_capture.sql` |
| Capture login | Authenticated non-owner, non-privileged login whose complete inherited-role closure is exactly `leo_capture` |
| Analysis login | Authenticated non-owner, non-privileged login whose complete inherited-role closure is exactly `leo_analysis` |
| Audit login | Disposable database owner, used only for read-only identity, migration, initial-closure, and final evidence queries |

Create separate, randomized login credentials. A representative database-owner
session uses:

```sql
CREATE ROLE wave7_capture LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS PASSWORD 'generated-secret';
GRANT leo_capture TO wave7_capture;

CREATE ROLE wave7_analysis LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS PASSWORD 'different-generated-secret';
GRANT leo_analysis TO wave7_analysis;

-- The qualification compares the pinned cluster identifier through every DSN.
-- This one read-only catalog function is the only direct object grant permitted
-- on either runtime login.
GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system()
  TO wave7_capture, wave7_analysis;
```

Do not give either login any other inherited role, database/object ownership, or
direct schema, table, sequence, database, or function grant. The harness
enumerates the authenticated `session_user`, its complete recursive membership
closure, direct ACLs, and owned database objects before it permits a write.

Store each DSN in a separate absolute-path, single-link, owner/root-owned regular
file no larger than 4096 bytes. It must be owner-readable with no group or other
permission bits; mode `0600` is recommended. Symlinks and malformed libpq
connection strings are rejected before a connection is opened.

Obtain the approved database name, database owner, and cluster system identifier
from the disposable-cluster provisioning receipt, not from a candidate endpoint
that the qualification is supposed to verify. The audit login can confirm the
identifier with:

```sql
SELECT current_database(), pg_get_userbyid(d.datdba),
       current_setting('server_version_num'), c.system_identifier
FROM pg_database AS d CROSS JOIN pg_control_system() AS c
WHERE d.datname = current_database();
```

After migrations have been applied, all public application tables must still be
empty. The harness checks this closure read-only before importing the qualified
source recording. A wrong database, owner, PostgreSQL major, system identifier,
migration byte, extra migration receipt, unexpected table, existing row, changed
session role, inherited role, direct grant, or owned object fails before any
catalog write, TX check, or radio open.

## Execution

Confirm there is no other IIO or capture process, confirm TX2 is muted, and
confirm host time synchronization. Run the qualified V5 image with host
networking and bind only the local source, local output, and credential
directory:

```console
python3 -m leo_flow.deployments.v5_production_path_e2e \
  --live \
  --output-root /run/e2e \
  --source-cas-root /run/source-cas \
  --capture-dsn-file /run/credentials/capture.dsn \
  --analysis-dsn-file /run/credentials/analysis.dsn \
  --audit-dsn-file /run/credentials/audit.dsn \
  --migration-directory /opt/leo-flow/migrations \
  --confirm-database-name wave7_approved_disposable \
  --confirm-database-owner wave7_database_owner \
  --confirm-system-identifier 7612345678901234567 \
  --confirm-radio-serial 104000b29905000e17000800065934759d \
  --confirm-clock-source host-ntp-synchronized
```

The source root is opened first and its kernel mount ID is matched to the exact
`/proc/self/mountinfo` row. For an absent output root, the nearest existing
ancestor is opened and required to match the explicit local-filesystem allowlist
before any directory is created; the created root is then opened and checked
again. A rejected ancestor remains absent, and a failed post-creation recheck
removes only the still-empty directories created by the harness. Using the
opened mount ID, rather than the first textual mount-point match, identifies the
effective mount when multiple mounts are stacked at the same path.

These gates prove the filesystem type and mount identity observed by this Linux
process at each check. They do not prove the physical topology beneath an
allowlisted local or stacked filesystem, and they do not prevent an external
administrator from changing mounts after preflight. TX2 mute is read before and
after the only radio operation. The fixed live request is 16 refills, 4,194,304
paired samples, approximately 2.097 seconds at 2 MHz.

## Passing evidence

`production-path-report.json` is written outside the CAS. A pass proves:

| Boundary | Evidence |
|---|---|
| Database identity | Every DSN observed the approved PostgreSQL 16 database, owner, and cluster system identifier |
| Schema | Exactly 20 immutable migration names and byte hashes ending in `0020`; no extra public application table |
| Authority | Separate authenticated capture and analysis session users; exact recursive role closure, no ownership, and only the approved `pg_control_system()` direct grant |
| Ingress | Stable request/job digest and same-content publication replay |
| Lease | Initial claim, expiry/reclaim generation increment, stale heartbeat rejection, active heartbeat, fenced completion |
| Capture | Stable plan/recording receipt, exact V5 continuity, re-hashed object pair, stopped health |
| Catalog | Exact final closure: two recordings, two FeatureSets, two durable feature-projection work items, six objects, one dwell ingress, three jobs, and zero rows in every other application table |
| Analysis | Source and dwell jobs atomically publish exact FeatureSets; both analysis jobs and the dwell job finish `succeeded` with result references |
| Replay | Fresh queue, supervisor, catalog, blob store, job repository, and worker instances neither recapture nor reanalyze |
| Cleanup | Capture spool is `cleaned` and contains no remaining recording files |
| Storage | Six output-CAS objects; opened source/output mount IDs reported allowlisted filesystem types and neither reported `nfs`/`nfs4` (a mount observation, not physical-topology proof) |
| RF safety | TX2 DDS scales remain zero and TX2 hardware gain remains `-80 dB` before and after |

The source and dwell analyses remain scientific observations of a passive
no-LNB baseline. A passing report makes no Starlink detection or ground-truth
claim.

## Hardware-free gate

```console
.venv/bin/pytest -q tests/integration/test_v5_production_path_e2e.py
```

The PostgreSQL tests use a disposable PostgreSQL 16 container, real randomized
scoped login roles, and a metadata-aware fake V5 radio. They exercise the
complete composition and fresh-process replay, plus fail-before-write cases for
wrong cluster identity, a nonempty database, and an unexpected direct runtime
grant, without contacting `.15`, NFS, or any persistent database.
