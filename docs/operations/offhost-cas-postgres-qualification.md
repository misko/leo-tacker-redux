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
  "schema_version": "0.1",
  "station_id": "station_example",
  "cas": {
    "root": "/var/lib/leo-flow/objects",
    "mount_source": "storage.example:/leo-flow-cas",
    "filesystem_type": "nfs4",
    "group_name": "leo-flow-cas"
  },
  "migration_directory": "/opt/leo-flow/migrations",
  "credential_names": {
    "leo_capture": "capture-catalog-dsn",
    "leo_analysis": "analysis-catalog-dsn",
    "leo_dashboard": "dashboard-catalog-dsn"
  },
  "pipeline": {
    "recording_id": "rec_replace_with_exact_published_id",
    "job_id": "job_replace_with_exact_submission_id"
  }
}
```

The CAS root must be absolute, normalized, non-root, and an exact mount point.
`mount_source` and `filesystem_type` are required operator assertions, not
values discovered and silently accepted by the harness. Use the stable backing
source as it appears in `/proc/self/mountinfo`; aliases are intentionally not
treated as equivalent. The root must be owned by the configured shared group,
have group `rwx` and setgid permissions, and the capture and analysis service
users must each be members of that group.

The configuration contains credential *names*, never DSNs. The commands resolve
only systemd credentials. `migration_directory` must contain the exact ordered
SQL files used to migrate the catalog; every corresponding `schema_migration`
name and SHA-256 receipt must match. Additional future receipts are reported as
compatible, but every migration known to this runtime is required.

Set `pipeline` to `null` for mount/role qualification before a recording exists.
Set exact IDs after publication and analysis. There is no `latest` lookup.

## Gate 1: read-only host inspection

Run capture inspection as the capture service identity with only the capture
credential loaded:

```console
systemd-run --wait --pipe --collect \
  --property=User=leo-capture \
  --property=SupplementaryGroups=leo-flow-cas \
  --property=LoadCredential=capture-catalog-dsn:/etc/leo-flow/secrets/capture-catalog-dsn \
  /opt/leo-flow/bin/python -m leo_flow.qualification.offhost \
  --config /etc/leo-flow/offhost-qualification.json \
  inspect --host-role capture
```

Run analysis inspection as the analysis service identity. It checks both the
analysis write capability and the dashboard's separate read-only credential:

```console
systemd-run --wait --pipe --collect \
  --property=User=leo-analysis \
  --property=SupplementaryGroups=leo-flow-cas \
  --property=LoadCredential=analysis-catalog-dsn:/etc/leo-flow/secrets/analysis-catalog-dsn \
  --property=LoadCredential=dashboard-catalog-dsn:/etc/leo-flow/secrets/dashboard-catalog-dsn \
  /opt/leo-flow/bin/python -m leo_flow.qualification.offhost \
  --config /etc/leo-flow/offhost-qualification.json \
  inspect --host-role analysis
```

Each command emits one JSON document. Exit `0` means all gates passed, exit `2`
means the inspection completed with at least one failed gate, and exit `3`
means input or infrastructure could not be safely inspected. Save stdout in an
operator evidence location outside the CAS. Reports are evidence only; no
service watches them.

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
