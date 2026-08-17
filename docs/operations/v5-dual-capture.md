# V5 dual-capture operator

For the ordered Gauss capture-to-analysis-to-dashboard procedure, use the
[Gauss local pipeline runbook](gauss-local-pipeline.md). This document retains
the detailed dual-operator boundary and recovery semantics.

The dual operator is a checked, machine-readable boundary for exactly two V5
station specifications. It supports independent comparison captures and
software-coordinated captures. Coordinated mode uses a common software release,
but eligibility is determined from measured first-sample UTC evidence; it does
not claim hardware synchronization.

Run it as `python -m leo_flow.deployments.v5_dual_capture_operator`. All JSON
written to stdout or an output file is canonical public-contract data. Errors
are bounded JSON events on stderr.

Create an independent batch without hand-authoring JSON:

```console
python -m leo_flow.deployments.v5_dual_capture_operator plan-batch \
  --station-a /absolute/radio-a.station.json \
  --station-b /absolute/radio-b.station.json \
  --mode independent \
  --batch-id cbatch_example \
  --attempt-a-id cattempt_example_a \
  --attempt-b-id cattempt_example_b \
  --requested-start-a-utc-ns 1780000000000000000 \
  --requested-start-b-utc-ns 1780000001000000000 \
  --output /absolute/new-batch.json
```

For coordinated mode, replace the two requested-start options with
`--common-requested-start-utc-ns` and add
`--maximum-observed-start-skew-ns`. The output path must be absolute and new;
the command uses exclusive creation and never overwrites an existing file. Its
stdout receipt contains the exact batch and pair digests needed for arming.

Use `validate` or `show-batch` with both station paths and the batch path before
capture. These commands do not read credentials, open a radio, inspect CAS, or
connect to a database.

```console
python -m leo_flow.deployments.v5_dual_capture_operator validate \
  --station-a /absolute/radio-a.station.json \
  --station-b /absolute/radio-b.station.json \
  --batch /absolute/new-batch.json
```

`capture` additionally requires `--arm`, both exact serial confirmations, the
batch and pair digest confirmations, `--credential-directory`, and an absolute
`--batch-database`. It acquires the stations' shared nonblocking pipeline-mode
lock before credentials, state, or radio construction and holds it through
capture cleanup, terminal SQLite persistence, and initial dashboard
publication. A publication outage returns `dual_capture_publication_failed`;
the terminal SQLite snapshot remains durable. Repeating the exact command
retries publication without rebuilding either capture runner.

Each live attempt is owned by a distinct fresh Python interpreter created with
the `spawn` start method. The child constructs its station cycle and exact
libiio/SPF stack only after it starts, completes preflight, reports readiness,
and waits for the parent executor's release decision. Independent mode releases
each child after its own readiness; coordinated mode releases both from the
existing common software gate. The catalog credential crosses only the private
spawn channel and is never placed in argv or an environment variable. Child
standard streams are isolated, ambient environment variables are scrubbed to a
small loader/locale allowlist, and child exceptions reduce to the existing
sanitized runner-failure outcome.

The parent remains the mode-lock, admission, batch-state, and initial dashboard
projection authority. Recording publication remains inside each exact one-shot
station attempt. Normal completion and every error path join both children
before projection and mode-lock release. Cancellation escalates through a
short cooperative request, terminate, and kill with explicit bounds. Linux
parent-death signaling kills a child if the lock-owning parent disappears, and
the parent supervisor performs an idempotent abort-all before releasing the
shared mode lock.

For a new batch, the operator resolves the `leo_capture` catalog credential
under that lock and asks PostgreSQL for one fail-closed capture-admission
decision before either runner, CAS access path, or radio is constructed. The
gate refuses pending or running latest recording dashboard state and any
ready, leased, or failed recording-analysis or feature-projection work;
succeeded and deliberately parked work are terminal. A false decision or
database error emits only `dual_capture_admission_blocked` and releases the
lock. Offline planning/validation never reads the credential or calls the
gate. A terminal replay does not call the admission gate because it constructs
no runner, CAS path, or radio and only retries projection of the already-durable
public snapshot.

```console
python -m leo_flow.deployments.v5_dual_capture_operator capture \
  --station-a /absolute/radio-a.station.json \
  --station-b /absolute/radio-b.station.json \
  --batch /absolute/new-batch.json \
  --arm --confirm-analysis-stopped \
  --confirm-radio-a-serial VERIFIED_SERIAL_A \
  --confirm-radio-b-serial VERIFIED_SERIAL_B \
  --confirm-batch-digest sha256:EXACT_BATCH_DIGEST \
  --confirm-pair-digest sha256:EXACT_PAIR_DIGEST \
  --credential-directory /absolute/credential-directory \
  --batch-database /absolute/capture-batches.sqlite3
```

Export the exact public snapshot for the separate analysis operator with:

```console
python -m leo_flow.deployments.v5_dual_capture_operator show-state \
  --batch-database /absolute/capture-batches.sqlite3 \
  --batch-id cbatch_example > /absolute/batch-snapshot.json
```

`show-state` validates the private SQLite row through the strict contract codec
and writes only the canonical `CaptureBatchSnapshot`; it does not read the
private schema directly and performs no station, radio, CAS, or credential I/O.

No example identity in this document approves or identifies radios at `.20` or
`.21`. Production station files must use independently verified device serials,
qualified V5 firmware identity, isolated mutable state and radio locks, one
shared CAS root, and one shared pipeline-mode lock.
