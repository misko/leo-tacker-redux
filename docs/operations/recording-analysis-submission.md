# Recording analysis submission

Recording-analysis submission is an analysis-side operator action. Capture
publishes one immutable recording pair and stops; it neither selects an
algorithm nor creates an analysis job. The operator command resolves the
published recording from PostgreSQL and enqueues one exact, content-addressed
`recording_analysis` request.

## Durable boundary

| Input | Authority | Submission rule |
|---|---|---|
| Recording | `PostgresRecordingCatalog` published row | Exact `recording_id`; missing or substituted identity fails before enqueue |
| Analyzer | Reviewed station scientific package | Exact artifact ID, SHA-256 digest, and schema; no default or `latest` selection |
| Configuration | Reviewed immutable configuration bytes | Exact artifact ID, SHA-256 digest, and schema |
| Dependencies | Reviewed immutable artifacts | Complete explicit list, including an explicit empty list |
| Output | Contract | Exactly `org.leo-flow.feature-set-bundle/0.1` |
| Job | `PostgresJobLeaseRepository` | Content-derived stable job ID; an exact retry leaves one PostgreSQL row |

The checked [schema](../../deploy/recording-analysis-submission/submission.schema.json)
and [non-runnable example](../../deploy/recording-analysis-submission/submission.example.json)
define the command. Replace every example identity with the approved values
from the same station package used to build the offline worker. In particular,
the example digest strings are intentionally invalid placeholders, not an
approved Quality/PSD or detection release.

The command accepts only a systemd credential name. It does not accept a DSN
value, environment-variable fallback, algorithm alias, configuration filename,
or implicit output schema. Every database connection first proves that its
login is a member of `leo_analysis`, then executes `SET ROLE leo_analysis`.

Run the command on the analysis/operator host through a transient systemd unit
so the DSN is not placed in JSON, process arguments, or logs:

```console
systemd-run --wait --pipe --collect \
  --property=LoadCredential=catalog-dsn:/etc/leo-flow/secrets/analysis-catalog-dsn \
  /opt/leo-flow/bin/python -m leo_flow.deployments.recording_submission_v1 \
  --config /etc/leo-flow/recording-submission.json
```

A successful command prints only the recording ID, stable job ID, and request
schema identity. Repeating the exact command is safe. Changing the recording,
algorithm, configuration, dependency set, or output contract creates a
different request and job identity.

## CAS binding and deployment boundary

PostgreSQL is the control plane, not the sample transport. Its recording row
contains opaque `cas:sha256:...` locators and immutable metadata; it does not
contain IQ bytes. The capture host's final SigMF files and SQLite spool remain
local recovery state and are not analysis inputs. The two published CAS objects
(raw data and canonical metadata) are the analysis inputs.

| Plane | Capture host | Analysis host | Required binding |
|---|---|---|---|
| Catalog and jobs | Publishes recording rows | Reads rows and leases jobs | Same PostgreSQL catalog |
| CAS objects | Writes `sha256/<prefix>/<digest>` | Opens the cataloged locators | Same mounted backing store at the exact configured `cas_root` |
| Local recovery | SigMF pair plus SQLite spool | No access | None; never use the spool or filenames as a work queue |

The V5 production scan requires `/var/lib/leo-flow/objects` to be a real mount
and refuses to start when it is merely a local directory. The analysis host
must mount the same authoritative backing store at that exact path and call
`build_station_plugin(..., cas_root=Path("/var/lib/leo-flow/objects"))`. Capture
publishes about two CAS objects per recording (data and metadata), rather than
creating a marker or independent transfer file for every segment. The
submission command intentionally does not copy sample files or turn the CAS
into a work queue.

The repository enforces the capture-side mount boundary but does not provision
the mount server/client or prove that both hosts resolve it to the same backing
store. That infrastructure still needs an operator qualification: publish a
known object on capture, verify its byte count and SHA-256 through the analysis
mount, then submit the recording. The PostgreSQL command cannot prove this byte
plane by itself.

The live V5 E2E harness uses a local filesystem store and in-memory control
plane in one process. It proves contract readability and idempotent logic, but
does not prove off-host CAS transport, PostgreSQL durability, or an operational
worker deployment. PostgreSQL integration tests separately prove durable
catalog lookup, role restriction, and idempotent queue insertion.
