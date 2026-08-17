# Off-host CAS and PostgreSQL qualification

This harness qualifies the infrastructure boundary between capture and offline
analysis without opening a radio or inventing an NFS control plane. Its default
operations are read-only. It verifies exact mount inputs, service-user access,
database migrations and roles, and one already-completed immutable chain:

```text
published recording -> exact recording_analysis job -> exact FeatureSet
                    -> exact FeatureSet observations in the dashboard projection
```

It never scans filenames, copies the capture spool, creates a marker, submits a
job, claims a job, projects a dashboard row, runs an analyzer, modifies a
database, changes a mount, or manages a service. The pipeline command verifies
evidence that the normal publisher, submission command, offline worker, and
projection writer have already produced.

## Inputs and safety boundary

Use the same byte-for-byte configuration on both hosts. A representative
configuration is:

```json
{
  "schema_id": "org.leo-flow.offhost-qualification",
  "schema_version": "0.4",
  "station_id": "station_example",
  "cas": {
    "root": "/var/lib/leo-flow/objects",
    "mount_source": "storage.example:/leo-flow-cas",
    "filesystem_type": "nfs4",
    "group_name": "leo-flow-cas",
    "mount_root": "/"
  },
  "migration_directory": "/opt/leo-flow/migrations",
  "credential_names": {
    "leo_capture": "capture-catalog-dsn",
    "leo_analysis": "analysis-catalog-dsn",
    "leo_dashboard": "dashboard-catalog-dsn",
    "postgres_audit": "postgres-audit-dsn"
  },
  "postgres": {
    "database_name": "leo_flow",
    "database_owner": "leo_catalog_owner",
    "server_major": 16,
    "system_identifier": "7612345678901234567",
    "migration_head": "0038_dashboard_surrogate_score_distributions.sql",
    "login_names": {
      "leo_capture": "leo_capture_station_login",
      "leo_analysis": "leo_analysis_station_login",
      "leo_dashboard": "leo_dashboard_station_login",
      "postgres_audit": "leo_catalog_audit_login"
    }
  },
  "pipeline": {
    "recording_id": "rec_replace_with_exact_published_id",
    "job_id": "job_replace_with_exact_submission_id"
  }
}
```

The CAS root must be absolute, normalized, non-root, and an exact mount point.
`mount_source`, `filesystem_type`, and `mount_root` are required operator
assertions, not values discovered and silently accepted by the harness. Use the
stable values as they appear in `/proc/self/mountinfo`; aliases are intentionally
not treated as equivalent. The root must be owned by the configured shared
group, have group `rwx` and setgid permissions, and the capture and analysis
service users must each be members of that group.

The configuration contains credential *names*, never DSNs. The commands resolve
only systemd credentials. `migration_directory` must contain the exact ordered
SQL files used to migrate the catalog; every corresponding `schema_migration`
name and SHA-256 receipt through `0038_dashboard_surrogate_score_distributions.sql`
must match. Missing, changed, extra, or forward migration receipts all fail
this release's gate.

Copy
`deploy/offhost-qualification/qualification.example.json` to the managed
configuration location on both hosts. The capture and analysis copies must be
byte-for-byte identical: do not create a capture variant and an analysis
variant, because their configuration digests are compared later. The adjacent
JSON Schema documents the closed input shape. Replace every `REPLACE_WITH_`
value before inspection; the dry-run preflight names any placeholder left in
place.

The site-specific inputs intentionally absent from the repository are:

| Input | Source of truth | Stored in qualification JSON |
|---|---|---|
| Station ID | Operator inventory | Yes |
| Exact CAS backing source, filesystem type, and mount root | Approved storage provisioning record, confirmed against `/proc/self/mountinfo` | Yes |
| PostgreSQL database name and database owner | Approved database provisioning record | Yes |
| PostgreSQL major version (`16`) and cluster system identifier | Approved cluster provisioning/backup record, confirmed with `server_version_num` and `pg_control_system()` | Yes |
| Required migration head (`0038_dashboard_surrogate_score_distributions.sql`) | Release manifest | Yes |
| Four exact, distinct authenticated login names | Database role provisioning record | Yes |
| PostgreSQL endpoint and login secret for each role | Secret-management owner | No; store each DSN in its named systemd credential |
| Exact published recording/job IDs | Catalog output from the conducted run | Yes, only after the pipeline exists |

The radio URI is not an input to this harness. No qualification command in this
module contacts a radio.

Set `pipeline` to `null` for mount/role qualification before a recording exists.
Set exact IDs after publication and analysis. There is no `latest` lookup.

## Gate 0: no-contact host preflight

Before loading credentials or relying on a mounted CAS, run the host-specific
dry-run on each host:

```console
/opt/leo-flow/bin/python -m leo_flow.qualification.offhost \
  --config /etc/leo-flow/offhost-qualification.json \
  preflight --host-role capture

/opt/leo-flow/bin/python -m leo_flow.qualification.offhost \
  --config /etc/leo-flow/offhost-qualification.json \
  preflight --host-role analysis
```

`preflight` reads only the named JSON file. It does not resolve systemd
credentials, inspect the CAS path or mount table, import a PostgreSQL endpoint,
open a socket, or contact the radio. Its JSON report lists the exact CAS,
migration-directory, database-role, and systemd-credential inputs that the
selected host will require. Capture requires `leo_capture`; analysis requires
both `leo_analysis` and the independent `leo_dashboard` read credential. Every
inspection also requires the separate `postgres_audit` credential. Exit
`0` means the configuration is syntactically ready for read-only inspection;
exit `2` means the report contains an unresolved or unsafe local input; exit `3`
means the configuration itself could not be loaded safely.

A passing dry-run is a plan, not infrastructure evidence. It does not assert
that a mount, credential, PostgreSQL endpoint, migration receipt, or catalog row
exists. Those are checked by the read-only `inspect` command below.

Do not populate the PostgreSQL or mount identity fields by accepting whatever a
candidate endpoint reports. Obtain the approved values independently, enter
them in the shared configuration, and let `inspect` compare the observation to
that pinned identity. This prevents a valid credential for the wrong database,
cluster, export root, or release from passing qualification.

## Gate 1: read-only host inspection

Run capture inspection as the capture service identity with the capture and
separate audit credentials loaded:

```console
systemd-run --wait --pipe --collect \
  --property=User=leo-capture \
  --property=SupplementaryGroups=leo-flow-cas \
  --property=LoadCredential=capture-catalog-dsn:/etc/leo-flow/secrets/capture-catalog-dsn \
  --property=LoadCredential=postgres-audit-dsn:/etc/leo-flow/secrets/postgres-audit-dsn \
  /opt/leo-flow/bin/python -m leo_flow.qualification.offhost \
  --config /etc/leo-flow/offhost-qualification.json \
  inspect --host-role capture
```

Run analysis inspection as the analysis service identity. It checks the
analysis write capability, the dashboard's separate read-only credential, and
the audit credential:

```console
systemd-run --wait --pipe --collect \
  --property=User=leo-analysis \
  --property=SupplementaryGroups=leo-flow-cas \
  --property=LoadCredential=analysis-catalog-dsn:/etc/leo-flow/secrets/analysis-catalog-dsn \
  --property=LoadCredential=dashboard-catalog-dsn:/etc/leo-flow/secrets/dashboard-catalog-dsn \
  --property=LoadCredential=postgres-audit-dsn:/etc/leo-flow/secrets/postgres-audit-dsn \
  /opt/leo-flow/bin/python -m leo_flow.qualification.offhost \
  --config /etc/leo-flow/offhost-qualification.json \
  inspect --host-role analysis
```

Each command emits one JSON document. Exit `0` means all gates passed, exit `2`
means the inspection completed with at least one failed gate, and exit `3`
means input or infrastructure could not be safely inspected. Save stdout in an
operator evidence location outside the CAS. Reports are evidence only; no
service watches them.

Database inspection begins read-only transactions. The audit credential alone
proves the exact database name, owner, PostgreSQL 16 major version, cluster
system identifier, and an inventory equal to the local migration receipt hashes
through `0027`; it may receive the separately reviewed monitoring access needed
for cluster identity. Runtime logins do not need `pg_monitor`. Each runtime
credential must authenticate as its exact configured `session_user`, must be a
distinct non-elevated login with exactly one application capability membership,
must own no database objects, and must have no direct ACL entries. In particular,
`leo_analysis` may publish dwell requests and `leo_capture` may lease or
transition them only through the approved security-definer routines; neither
role receives direct access to `dwell_request_ingress`. The analysis role also
publishes, leases, and transitions durable FeatureSet projection work only
through the `0020` security-definer routines; capture, analysis, and dashboard
roles receive no direct access to `feature_projection_work`.

Transfer the two evidence documents through the operator management plane and
compare them on either host:

```console
/opt/leo-flow/bin/python -m leo_flow.qualification.offhost \
  --config /etc/leo-flow/offhost-qualification.json \
  compare-hosts \
  --capture-report /var/tmp/capture-host-report.json \
  --analysis-report /var/tmp/analysis-host-report.json
```

This rejects different station/config identities, CAS roots, backing sources,
filesystem types, roles, or failed host gates. The host-local device number is
recorded for diagnosis but is not compared because the same remote filesystem
can have different device numbers in separate mount namespaces.

## Gate 2: explicitly armed cross-host byte probe

The read-only gate proves configuration and effective local access. To prove
that bytes written by one service identity are visible through the other
host's mount, use the optional probe. This is the only write operation. It
requires both `--arm-writes` and an exact repetition of the configured CAS
root. Omitting either makes no filesystem change.
Immediately before creating the normal CAS writer, the command repeats the
exact mount, backing source, filesystem type, group, permission, and effective
access checks. If the shared mount has disappeared and exposed an underlying
local directory, the probe fails without creating `.tmp` or an object.

On the capture host, write one bounded immutable content-addressed object:

```console
/opt/leo-flow/bin/python -m leo_flow.qualification.offhost \
  --config /etc/leo-flow/offhost-qualification.json \
  write-probe --host-role capture \
  --probe-id offhost_20260814_capture_a \
  --arm-writes \
  --confirm-cas-root /var/lib/leo-flow/objects
```

Pass the JSON receipt—not a path—through the operator management plane. On the
analysis host, read that exact `ObjectRef`:

```console
/opt/leo-flow/bin/python -m leo_flow.qualification.offhost \
  --config /etc/leo-flow/offhost-qualification.json \
  read-probe --host-role analysis \
  --probe-receipt /var/tmp/capture-probe-receipt.json
```

Repeat in the reverse direction with a new probe ID: analysis writes and
capture reads. That proves both cross-user directions. A probe is at most 4096
bytes, has a canonical `cas:sha256:` locator, and is verified by digest and
byte count through the normal filesystem CAS adapter. The receipt carries no
constructed storage path, and the CAS object is never used as a signal or work
queue entry.

The harness deliberately does not delete immutable CAS data. Probe objects are
unregistered qualification artifacts; retain their receipts and let the
approved unregistered-object reconciliation/retention process handle them. Do
not manually remove a digest path.

## Gate 3: exact durable pipeline closure

After capture publication, use the separately documented recording submission
command, run the approved offline worker, and run the normal projection writer.
Then pin the resulting recording and stable job IDs in `pipeline` and execute
from the analysis host with analysis and dashboard credentials loaded:

```console
/opt/leo-flow/bin/python -m leo_flow.qualification.offhost \
  --config /etc/leo-flow/offhost-qualification.json \
  verify-pipeline
```

The command starts PostgreSQL transactions read-only and checks:

- the exact published recording row and its two cataloged CAS references;
- that the exact typed job embeds the complete published `RecordingObjectRef`;
- that the job succeeded and its exact result digest resolves one FeatureSet;
- that the standard remote recording reader validates metadata and data through
  the configured shared CAS root;
- that the FeatureSet bundle digest, IDs, recording identity, and catalog row
  agree; and
- that the latest immutable dashboard projections for that recording contain
  exactly the FeatureSet observation IDs and each projection identity is bound
  to the selected FeatureSet reference.

The command does not make a failed or pending job succeed. If any stage is not
complete, leave the evidence intact and diagnose the owning publisher, worker,
or projection writer.

## Hardware-free gate

Run the component-owned suite before using the operator commands:

```console
.venv/bin/pytest -q tests/qualification/test_offhost.py
```

The tests use temporary directories and fake PostgreSQL sessions. They do not
contact a database, mount, service manager, network host, or radio. They prove
dry-run non-mutation, exact mount rejection, receipt hashing, role and privilege
inspection, dual arming, bounded content-addressed probes, cross-role reads, and
the full identity closure used by the pipeline report.
