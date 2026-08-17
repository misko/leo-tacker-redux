# Gauss local capture, analysis, and dashboard runbook

This is the sequencing authority for the Gauss V5 pipeline. Linked component
runbooks remain authoritative for their detailed evidence. Commands marked
**LIVE — DO NOT RUN UNTIL ADMITTED** can contact a radio or mutate durable
state; preparation commands are radio-free.

## Checkpoints

| Gate | Pass evidence | Stop when |
|---|---|---|
| G0 runtime | CPython 3.11.16, frozen lock, scripts installed | Python, lock, source, or science approval differs |
| G1 storage | qualified PG16 cluster/roles/migrations through `0031`; qualified CAS; separate credentials | a DSN leaks, identity/receipt differs, or a role/mount check fails |
| G2 offline | `.15`, `.20`, and `.21` station/plan/V5 runtime plus Gauss science validation pass | any digest, path, firmware/runtime binding, or identity is unreviewed |
| G3 admission | queues drained, shared mode lock free, capacity healthy, exact arms recorded | work is ready/leased/failed, lock is held, or a serial was not observed |
| G4 capture | terminal recording or two-attempt public snapshot is durable | continuity is unverified or a partial batch would be recaptured |
| G5 analysis | exact submission and bounded job/projection drains complete | a bound is reached, work fails, or projection remains unavailable |
| G6 dashboard | loopback preflight, V1/V2 JSON, and UI show the exact results | read-only role, route, or browser view disagrees with durable facts |

## 1. Install the exact environment

The checked science approval pins CPython 3.11.16 and `uv.lock`:

```console
uv python install 3.11.16
uv sync --frozen --python 3.11.16 \
  --extra hardware --extra server --extra format --extra orbit --extra dev
uv run --python 3.11.16 python --version
uv run --python 3.11.16 leo-v5-capture --help
uv run --python 3.11.16 leo-v5-dual-capture --help
uv run --python 3.11.16 leo-v5-campaign --help
uv run --python 3.11.16 leo-gauss-analysis --help
uv run --python 3.11.16 leo-dashboard --help
```

The radio also needs the separately qualified native libiio/pyadi/SPF runtime
recorded in the V5 manifest; a wheel-only environment does not replace its
attestation. Python 3.14 is useful only as a control-plane side-test. It is not
the approved Gauss science runtime. On Linux `fcntl`/`flock` is part of
CPython, not a PyPI package.

## 2. Qualify PostgreSQL 16, CAS, and credentials

Start the site-managed PostgreSQL 16 unit. Never put a DSN in JSON or argv.
Keep each role's DSN in a mode-`0600` source file and expose it as the systemd
credential `catalog-dsn`: `leo_capture` for capture, `leo_analysis` for
analysis, and read-only `leo_dashboard` for the dashboard.

Run the no-contact plans first:

```console
uv run --python 3.11.16 python -m leo_flow.qualification.offhost \
  --config /etc/leo-flow/offhost-qualification.json \
  preflight --host-role capture
uv run --python 3.11.16 python -m leo_flow.qualification.offhost \
  --config /etc/leo-flow/offhost-qualification.json \
  preflight --host-role analysis
```

Then run the read-only service-identity `inspect` commands with
`LoadCredential=` exactly as documented in
[off-host qualification](offhost-cas-postgres-qualification.md). Require PG16,
the independently recorded cluster identity, exact migration receipts through
`0030_campaign_scoped_analysis_claims.sql`, least-privilege
roles, and the
same qualified CAS.

## 3. Validate the development canary and authoritative science pair offline

The checked development identities are `.15` / `radio_pluto_v5_canary_15`,
`.20` / `radio_pluto_5d4d`, and `.21` / `radio_pluto_19f2`. The IP addresses
route the connection; the exact serials in each station document establish
identity. Every command in this section is offline; `validate-runtime` attests
the current Python/native runtime and selected transport backend without
creating a radio context:

```console
uv run --python 3.11.16 leo-v5-capture validate \
  --station deploy/v5-scan/development-radio-15.station.json
uv run --python 3.11.16 leo-v5-capture show-plan \
  --station deploy/v5-scan/development-radio-15.station.json
uv run --python 3.11.16 leo-v5-capture validate-runtime \
  --station deploy/v5-scan/development-radio-15.station.json
uv run --python 3.11.16 leo-v5-capture validate \
  --station deploy/v5-scan/gauss-r20-science-postreboot-passive-v1.station.json
uv run --python 3.11.16 leo-v5-capture validate-runtime \
  --station deploy/v5-scan/gauss-r20-science-postreboot-passive-v1.station.json
uv run --python 3.11.16 leo-v5-capture validate \
  --station deploy/v5-scan/gauss-r21-science-postreboot-passive-v1.station.json
uv run --python 3.11.16 leo-v5-capture validate-runtime \
  --station deploy/v5-scan/gauss-r21-science-postreboot-passive-v1.station.json
uv run --python 3.11.16 leo-gauss-analysis validate \
  --config deploy/gauss-analysis-v1/analysis.json \
  --science-manifest deploy/gauss-analysis-v1/science.json
```

Run `validate-runtime` inside the native environment described by
[the V5 scan runbook](v5-scan.md). It does not attest the endpoint's live
serial, firmware, or RF state; that remains a separate live preflight before
capture. Preserve the offline runtime receipts.

## 4. Single-radio capture

The single operator executes one restart-safe `OneShotV5PlanCycle`; it is not
a polling capture queue. Preflight precedes radio access, the immutable plan is
captured once, and exact replay reconciles durable state without recapture.

**LIVE — DO NOT RUN UNTIL ADMITTED:**

```console
uv run --python 3.11.16 leo-v5-capture capture \
  --station deploy/v5-scan/development-radio-15.station.json \
  --arm \
  --confirm-radio-serial OBSERVED_EXACT_SERIAL \
  --confirm-plan-digest sha256:REVIEWED_EXACT_PLAN_DIGEST \
  --credential-directory /run/credentials/leo-v5-scan
```

The serial must be independently observed and the digest copied from the
offline receipt. Admission acquires the nonblocking shared mode lock and asks
PG to refuse capture while analysis/projection work is active or failed.
The checked `.20` and `.21` single plans already have terminal qualification
state; exact replay is safe, but a new observation requires a reviewed station
document with a fresh plan/activity/segment namespace and disjoint mutable
state. Never edit or delete terminal spool state to force recapture.

## 5. Create and inspect a dual batch

The checked `.20`/`.21` identities and their successful independent attempt-2
evidence are retained in
`gauss-dual-independent-attempt2-radio-20-pluto-5d4d.station.json` and
`gauss-dual-independent-attempt2-radio-21-pluto-19f2.station.json`. Those plan
identities are terminal evidence and must not be reused for a new observation.
Create a newly reviewed pair with fresh plan/activity/segment/state/spool/lock
identities. The pair must still share the exact CAS root and pipeline-mode lock.

Independent comparison:

```console
uv run --python 3.11.16 leo-v5-dual-capture plan-batch \
  --station-a /absolute/reviewed/fresh-radio-20.station.json \
  --station-b /absolute/reviewed/fresh-radio-21.station.json \
  --mode independent --batch-id cbatch_REVIEWED_ID \
  --attempt-a-id cattempt_REVIEWED_20_ID \
  --attempt-b-id cattempt_REVIEWED_21_ID \
  --requested-start-a-utc-ns REVIEWED_UTC_NS_20 \
  --requested-start-b-utc-ns REVIEWED_UTC_NS_21 \
  --output /absolute/new/independent-batch.json
```

Software-coordinated capture:

```console
uv run --python 3.11.16 leo-v5-dual-capture plan-batch \
  --station-a /absolute/reviewed/fresh-radio-20.station.json \
  --station-b /absolute/reviewed/fresh-radio-21.station.json \
  --mode coordinated --batch-id cbatch_REVIEWED_ID \
  --attempt-a-id cattempt_REVIEWED_20_ID \
  --attempt-b-id cattempt_REVIEWED_21_ID \
  --common-requested-start-utc-ns REVIEWED_COMMON_UTC_NS \
  --maximum-observed-start-skew-ns REVIEWED_MAXIMUM_SKEW_NS \
  --output /absolute/new/coordinated-batch.json
```

The output is exclusively created and never overwritten. Save its batch/pair
digest receipt. `create-batch` is an exact alias for `plan-batch`; it does not
open either radio. Run both offline inspections:

```console
uv run --python 3.11.16 leo-v5-dual-capture validate \
  --station-a /absolute/reviewed/fresh-radio-20.station.json \
  --station-b /absolute/reviewed/fresh-radio-21.station.json \
  --batch /absolute/new/selected-batch.json
uv run --python 3.11.16 leo-v5-dual-capture show-batch \
  --station-a /absolute/reviewed/fresh-radio-20.station.json \
  --station-b /absolute/reviewed/fresh-radio-21.station.json \
  --batch /absolute/new/selected-batch.json
```

Coordinated mode is software coordination, not hardware synchronization. A
common software release is used, but paired eligibility requires measured V5
first-sample UTC evidence within the declared skew bound. Independent mode
authorizes an identity-bound comparison without asserting synchronization.

## 6. Execute the dual batch

**LIVE — DO NOT RUN UNTIL THE FRESH `.20`/`.21` PAIR IS REVIEWED AND
ADMITTED:**

```console
uv run --python 3.11.16 leo-v5-dual-capture capture \
  --station-a /absolute/reviewed/fresh-radio-20.station.json \
  --station-b /absolute/reviewed/fresh-radio-21.station.json \
  --batch /absolute/new/selected-batch.json \
  --arm --confirm-analysis-stopped \
  --confirm-radio-a-serial OBSERVED_SERIAL_FOR_RADIO_20 \
  --confirm-radio-b-serial OBSERVED_SERIAL_FOR_RADIO_21 \
  --confirm-batch-digest sha256:EXACT_BATCH_DIGEST_FROM_RECEIPT \
  --confirm-pair-digest sha256:EXACT_PAIR_DIGEST_FROM_RECEIPT \
  --credential-directory /run/credentials/leo-v5-dual-capture \
  --batch-database /absolute/state/capture-batches.sqlite3
```

Exactly two attempts run concurrently and both terminal outcomes are recorded;
a solo success survives peer failure. The mode lock covers preflight, capture,
publication, close, terminal SQLite persistence, and initial dashboard view.

## 7. Export, submit, and drain locally

Use the public codec, never the private SQLite schema:

```console
uv run --python 3.11.16 leo-v5-dual-capture show-state \
  --batch-database /absolute/state/capture-batches.sqlite3 \
  --batch-id cbatch_REVIEWED_ID \
  > /absolute/evidence/cbatch_REVIEWED_ID.snapshot.json
uv run --python 3.11.16 leo-gauss-analysis submit-batch \
  --batch-snapshot /absolute/evidence/cbatch_REVIEWED_ID.snapshot.json \
  --science-manifest deploy/gauss-analysis-v1/science.json \
  --credential-directory /run/credentials/gauss-analysis
uv run --python 3.11.16 leo-gauss-analysis drain-batch \
  --batch-snapshot /absolute/evidence/cbatch_REVIEWED_ID.snapshot.json \
  --config deploy/gauss-analysis-v1/analysis.json \
  --science-manifest deploy/gauss-analysis-v1/science.json \
  --credential-directory /run/credentials/gauss-analysis \
  --max-analysis-jobs 4 --max-projection-work 4
```

`drain-batch` is bounded application work, not a daemon or shell workflow. If
either final `*_no_claimable_work` value is false, inspect durable queue state
before choosing a new bound. Analysis resolves exact catalog references and
never scans CAS paths.

The descriptive waterfall is a separate post-capture product. For every
successful `recording_id` in the public snapshot, enqueue it only after capture
has closed, then alternate the bounded analysis and projection commands until
both report `forward_progress: false` after the durable receipt is `succeeded`:

```console
uv run --python 3.11.16 leo-gauss-analysis submit-waterfall \
  --recording-id rec_EXACT_PUBLISHED_RECORDING \
  --science-manifest deploy/gauss-analysis-v1/science.json \
  --credential-directory /run/credentials/gauss-analysis
uv run --python 3.11.16 leo-gauss-analysis process-waterfall-one \
  --science-manifest deploy/gauss-analysis-v1/science.json \
  --credential-directory /run/credentials/gauss-analysis
uv run --python 3.11.16 leo-gauss-analysis project-waterfall-one \
  --science-manifest deploy/gauss-analysis-v1/science.json \
  --credential-directory /run/credentials/gauss-analysis
```

These commands all take the same nonblocking capture/analysis mode lock.
Generation reads the exact published recording from CAS; dashboard projection
reads the exact cataloged waterfall through the analysis-owned reader and never
exposes a CAS locator. The finite and continuous Gauss campaign composition
submits and proves both recordings' FeatureSet and waterfall projections before
marking a batch analyzed.

## 8. Start and verify the loopback dashboard

With the `leo_dashboard` `catalog-dsn` credential loaded:

```console
uv run --python 3.11.16 leo-dashboard \
  --config deploy/dashboard-v1/dashboard.json --forever
```

Keep `127.0.0.1:8090` loopback-only. In another terminal:

```console
curl --fail --silent http://127.0.0.1:8090/api/storage-health
curl --fail --silent 'http://127.0.0.1:8090/api/recordings?start_utc_ns=START_NS&stop_utc_ns=STOP_NS'
curl --fail --silent 'http://127.0.0.1:8090/api/v2/capture-batches?start_utc_ns=START_NS&stop_utc_ns=STOP_NS'
```

Open `http://127.0.0.1:8090/` and confirm the batch plus both feature results.
The locked radio-free real-browser proof is:

```console
uv run --python 3.11.16 playwright install chromium
uv run --python 3.11.16 pytest -q tests/e2e/test_dashboard_browser.py
```

See [Dashboard v1](dashboard-v1.md) for systemd credentials, role enforcement,
SSH forwarding, and authenticated TLS proxying.

## 9. Finite coordinated `.20`/`.21` campaign

The campaign operator is a Python application, not a shell workflow or a
daemon. It owns one component SQLite journal, one campaign-instance lock, and
one finite loop. Capture and analysis take the shared pipeline-mode lock in
separate stages, so local analysis never overlaps radio capture. The checked
Gauss runtime config contains only absolute paths and the two routing IPs;
database credentials remain mode-`0600` files resolved by name.

The post-hard-reboot science pair uses the base V5 radio firmware with the
already pinned host-prime runtime. Fresh ordinary and metadata diagnostics
showed varying dual-channel I/Q on both radios; the capture-owned
constant-component gate remains mandatory. The staged rx-integrity firmware is
fallback evidence only and is not part of the active science composition.

The current station pair additionally requires both TX hardware gains at or
below `-80 dB` and all eight DDS scales equal to zero. Armed preflight reads
these values from the selected radio context before releasing capture metadata
I/O. A mismatch, missing channel, constant component, serial/firmware/runtime
drift, or measured first-sample skew above 100 ms terminalizes the fresh unit.

The reviewed offline qualification is already materialized with fresh IDs.
Validate it without credentials, database, CAS, or radio contact:

```console
leo-v5-campaign validate \
  --definition deploy/gauss-campaign-r20-r21-postreboot-v1/qualification-v5/qualification.definition.json \
  --station-a deploy/v5-scan/gauss-r20-science-postreboot-passive-v1.station.json \
  --station-b deploy/v5-scan/gauss-r21-science-postreboot-passive-v1.station.json \
  --campaign-state-root /home/mouse9911/.local/state/leo-flow/campaigns/qual_gauss_r20_r21_20260816_v5
python -m leo_flow.deployments.gauss_qualification_materialization validate \
  --manifest deploy/gauss-campaign-r20-r21-postreboot-v1/qualification-v5/qualification.materialization.json
```

The private V2 qualification definition encodes a fixed nine-cell no-catch-up
grid. Unit 0 is requested at `2026-08-16T20:30:00Z`; the remaining units are
requested at exact `start + floor(i × 400,000,000,000 / 3) ns` instants. Each
unit may begin preflight 15 seconds before its own requested start and becomes
terminally missed 5 seconds after it. Invocation time never replaces a stored
requested UTC, including after restart.

`validate` is radio-, database-, CAS-, credential-, and journal-free. Before
arming, stop and runtime-mask every service configured with `.20` or `.21`,
leave only the separately checked `.15` service, verify no process or TCP owner
of either campaign IP remains, and retain that evidence. The live operator
rechecks this ownership gate before and after every capture.

```console
leo-v5-campaign --runtime-config deploy/gauss-campaign-r20-r21-postreboot-v1/runtime.json run \
  --definition deploy/gauss-campaign-r20-r21-postreboot-v1/qualification-v5/qualification.definition.json \
  --station-a deploy/v5-scan/gauss-r20-science-postreboot-passive-v1.station.json \
  --station-b deploy/v5-scan/gauss-r21-science-postreboot-passive-v1.station.json \
  --journal /home/mouse9911/.local/state/leo-flow/campaigns/qual_gauss_r20_r21_20260816_v5/campaign.sqlite3 \
  --campaign-state-root /home/mouse9911/.local/state/leo-flow/campaigns/qual_gauss_r20_r21_20260816_v5 \
  --campaign-lock /home/mouse9911/.local/state/leo-flow/campaigns/qual_gauss_r20_r21_20260816_v5/campaign.lock \
  --capacity-margin-bytes 10737418240 \
  --arm --confirm-definition-digest sha256:9a1091b07917a74e815cbba3a64283450d63f06a89513afeb135e7c9ffeb72fc
leo-v5-campaign qualification-receipt \
  --definition deploy/gauss-campaign-r20-r21-postreboot-v1/qualification-v5/qualification.definition.json \
  --journal /home/mouse9911/.local/state/leo-flow/campaigns/qual_gauss_r20_r21_20260816_v5/campaign.sqlite3 \
  --issued-utc-ns OBSERVED_UTC_NS \
  > /home/mouse9911/.local/state/leo-flow/campaigns/qual_gauss_r20_r21_20260816_v5/qualification.receipt.json
```

Any terminal qualification failure stops the qualification; preserve it and
plan a fresh identity after diagnosis. A complete receipt binds the nine exact
unit, terminal-snapshot, analysis, and projection receipts and is required by
the main definition.

Plan the main window only after qualification. Its fixed schedule contains 104
complete rounds, with each round containing all nine
`(1.25, 2.5, 5 MS/s) × (40, 80, 160 ms)` cells exactly once. That is 936
successful coordinated batches and 1,872 exact recording-analysis/projection
receipts. Targets use the rational no-drift grid
`start + floor(i × 400,000,000,000 / 13) ns`; missed slots are never compressed
into a catch-up burst.

The independent four-phase geometry is `L/L`, `L/U`, `U/U`, `U/L`. Because
the nine-cell and four-geometry periods are coprime, every cell/geometry pair
appears exactly 26 times. This balances same-edge replication, opposite-edge
diversity, and which radio visits each edge first.

```console
leo-v5-campaign plan-main \
  --campaign-id main_gauss_v5_REVIEWED_ID \
  --start-utc-ns REVIEWED_FUTURE_UTC_NS \
  --maximum-start-lateness-ns REVIEWED_LATENESS_NS \
  --station-a deploy/v5-scan/gauss-r20-science-postreboot-passive-v1.station.json \
  --station-b deploy/v5-scan/gauss-r21-science-postreboot-passive-v1.station.json \
  --qualification-definition /absolute/new/qualification.definition.json \
  --qualification-receipt /absolute/new/qualification.receipt.json \
  --deferred-analysis \
  --output /absolute/new/main.definition.json
leo-v5-campaign validate \
  --definition /absolute/new/main.definition.json \
  --qualification-receipt /absolute/new/qualification.receipt.json \
  --station-a deploy/v5-scan/gauss-r20-science-postreboot-passive-v1.station.json \
  --station-b deploy/v5-scan/gauss-r21-science-postreboot-passive-v1.station.json \
  --campaign-state-root /absolute/new/main-state \
  --deferred-analysis
```

Each unit starts radio preflight 15 seconds before its target, waits for both
fresh process-isolated attempts to finish exact first-segment configuration and
readback before becoming ready, then admits both through one software common
release. MetadataBuffer open and READBUFM remain after release. The checked
Gauss runtime delays the secondary child dispatch by 10 ms to space iiOD
metadata commands after observed concurrent EIO/EBUSY failures. Each
configuration phase permits at most two full writes. Every write receives a
bounded 250 ms exact-readback window with 10 ms polling; only mismatch
exhaustion may close the unused metadata state, destroy the RX buffer, wait
50 ms, and issue the final write. A verified metadata segment permits one
separate 50 ms recovery and one fresh buffer attempt only for `EIO` or `EBUSY`
before any refill is accepted. Later, semantic, interleave, callback, other
transport, setter/getter I/O, and cleanup failures remain terminal. Successful
manifests report the exact configuration-write, readback, and transport attempt
counts.
Eligibility still uses measured first-sample evidence with a fixed 100 ms
bound; this is not a hardware synchronization claim. The 1.25 MS/s cells are
explicitly tagged clipped-pilot/do-not-pool and do not weaken the existing
full-pilot scan validation. `status` reads only the exact definition and
journal and reports the authoritative nine-cell vector (104 successes per
cell). Dashboard V2 reports
batch/recording/result progress but does not infer campaign-cell balance from
plan IDs.

## 10. Staged R20/R21 capture-first collection and deferred drain

The R20/R21 deployment uses two systemd services over one immutable main
definition, one qualification receipt, one SQLite journal, and one campaign
lock:

| Unit | Allowed phase | Successful terminal state |
| --- | --- | --- |
| `leo-v5-continuous-r20-r21-capture.service` | capture only | journal has durably entered `analyzing` after 936 successful batches |
| `leo-v5-continuous-r20-r21-analysis.service` | analysis only | journal is `complete` with 936 analysis receipts |

The capture unit cannot invoke analysis. On successful capture closure,
`OnSuccess=` starts the analysis unit only after the campaign lock has been
released. The analysis unit cannot run while the journal is still `capturing`.
Both units use the same campaign lock, and the capture and analysis adapters
continue to take the shared pipeline-mode lock in their separate phases.

The capture invocation is bounded to 1,873 loop transitions and 9 hours:
each of the 936 scheduled batches can require one durable plan/`not_due`
transition and one capture transition, followed by one phase-close transition.
The analysis invocation remains bounded to 937 transitions and 9 hours: one
per captured batch plus one completion transition. Exit status `75` means the
bounded slice ended with durable work still pending; systemd restarts that same
mode against the same journal. Exit status `0` means that mode reached its
required terminal phase. Configuration, arm, or terminal campaign failures
use statuses `2`, `3`, or `4`; the unit explicitly does not restart those
failures. This permits a long local analysis drain to span several bounded
processes without recapture or identity replacement.

After the exact R20/R21 qualification reports all 9 cells successful, emit its
immutable receipt once. `REVIEWED_RECEIPT_UTC_NS` must be the reviewed current
UTC time in nanoseconds, and the destination must not already exist:

```console
umask 077
set -o noclobber
.venv/bin/leo-v5-campaign qualification-receipt \
  --definition deploy/gauss-campaign-r20-r21-postreboot-v1/qualification-v5/qualification.definition.json \
  --journal /home/mouse9911/.local/state/leo-flow/campaigns/qual_gauss_r20_r21_20260816_v5/campaign.sqlite3 \
  --issued-utc-ns REVIEWED_RECEIPT_UTC_NS \
  > /home/mouse9911/.local/state/leo-flow/campaigns/qual_gauss_r20_r21_20260816_v5/qualification.receipt.json
set +o noclobber
```

Then make the new main state directory and plan the main definition with the
same source station pair. `REVIEWED_FUTURE_UTC_NS` must be later than the
receipt issue time:

```console
install -d -m 0700 \
  /home/mouse9911/.local/state/leo-flow/campaigns/main_gauss_r20_r21_20260816_v1
env \
  PYTHONPATH=/home/mouse9911/.cache/leo-flow/v5-runtime-rx-integrity-candidate1/lib/python3.11/site-packages:/home/mouse9911/.cache/leo-flow/v5-build/spf-rx-integrity-candidate1 \
  LD_LIBRARY_PATH=/home/mouse9911/.cache/leo-flow/v5-runtime-rx-integrity-candidate1/lib \
  .venv/bin/leo-v5-campaign plan-main \
  --campaign-id main_gauss_r20_r21_20260816_v1 \
  --start-utc-ns REVIEWED_FUTURE_UTC_NS \
  --maximum-start-lateness-ns 100000000 \
  --station-a deploy/v5-scan/gauss-r20-science-postreboot-passive-v1.station.json \
  --station-b deploy/v5-scan/gauss-r21-science-postreboot-passive-v1.station.json \
  --qualification-definition deploy/gauss-campaign-r20-r21-postreboot-v1/qualification-v5/qualification.definition.json \
  --qualification-receipt /home/mouse9911/.local/state/leo-flow/campaigns/qual_gauss_r20_r21_20260816_v5/qualification.receipt.json \
  --deferred-analysis \
  --output /home/mouse9911/.local/state/leo-flow/campaigns/main_gauss_r20_r21_20260816_v1/main.definition.json
.venv/bin/leo-v5-campaign validate \
  --definition /home/mouse9911/.local/state/leo-flow/campaigns/main_gauss_r20_r21_20260816_v1/main.definition.json \
  --qualification-receipt /home/mouse9911/.local/state/leo-flow/campaigns/qual_gauss_r20_r21_20260816_v5/qualification.receipt.json \
  --station-a deploy/v5-scan/gauss-r20-science-postreboot-passive-v1.station.json \
  --station-b deploy/v5-scan/gauss-r21-science-postreboot-passive-v1.station.json \
  --campaign-state-root /home/mouse9911/.local/state/leo-flow/campaigns/main_gauss_r20_r21_20260816_v1
```

Record the planner-emitted main definition digest only after the offline
validation succeeds. Install fresh copies of both reviewed units, replacing
the placeholder in each copy with that one exact digest:

```console
install -m 0644 \
  deploy/gauss-campaign-r20-r21-postreboot-v1/leo-v5-continuous-r20-r21-capture.service \
  /etc/systemd/system/leo-v5-continuous-r20-r21-capture.service
install -m 0644 \
  deploy/gauss-campaign-r20-r21-postreboot-v1/leo-v5-continuous-r20-r21-analysis.service \
  /etc/systemd/system/leo-v5-continuous-r20-r21-analysis.service
R20_R21_MAIN_DIGEST=sha256:REVIEWED_EXACT_MAIN_DEFINITION_DIGEST
sed -i "s/REPLACE_WITH_EXACT_MAIN_DEFINITION_DIGEST/${R20_R21_MAIN_DIGEST}/g" \
  /etc/systemd/system/leo-v5-continuous-r20-r21-capture.service \
  /etc/systemd/system/leo-v5-continuous-r20-r21-analysis.service
systemd-analyze verify \
  /etc/systemd/system/leo-v5-continuous-r20-r21-capture.service \
  /etc/systemd/system/leo-v5-continuous-r20-r21-analysis.service
systemctl daemon-reload
systemctl enable --now leo-v5-continuous-r20-r21-capture.service
```

Do not enable the analysis unit independently; capture closure starts it. An
operator may safely resume an interrupted deferred phase with
`systemctl start leo-v5-continuous-r20-r21-analysis.service` only after the
status command below reports `phase: analyzing`. Never change the definition,
receipt, station paths, journal, or digest during a resume.

```console
.venv/bin/leo-v5-continuous status \
  --definition /home/mouse9911/.local/state/leo-flow/campaigns/main_gauss_r20_r21_20260816_v1/main.definition.json \
  --journal /home/mouse9911/.local/state/leo-flow/campaigns/main_gauss_r20_r21_20260816_v1/continuous.sqlite3
systemctl status leo-v5-continuous-r20-r21-capture.service \
  leo-v5-continuous-r20-r21-analysis.service --no-pager
```

Final acceptance requires `phase=complete`, `captured_count=936`, and
`analyzed_count=936`. For every one of the 1,872 recording IDs, the dashboard
detail endpoint `/api/v3/recordings/{recording_id}` must report complete
analysis and the projected waterfall endpoint
`/api/v3/recordings/{recording_id}/waterfall` must report `state=complete` with
non-empty bounded tiles. The V2 batch view must agree for both recordings in
each coordinated batch. During the capture phase, immutable detail may be
visible with pending analysis; absence of a completed waterfall is expected
until the deferred phase projects it.

## Replay, recovery, and stop rules

- Exact single replay reconciles the durable plan without recapture.
- Exact terminal dual replay retries initial dashboard publication without
  radio construction. A partial stored batch is refused.
- Exact `submit-batch` replay is idempotent. Never edit a snapshot to force
  eligibility. Re-run bounded drain only after reviewing failed/delayed/parked
  work.
- A coordinated pair outside its measured skew bound retains both recordings
  but is not paired-analysis eligible.
- Missing/malformed state, unverified continuity, identity mismatch, held lock,
  projection outage, failed admission, or unresolved `.20`/`.21` evidence is a
  stop condition—not permission to scan storage or invent an identity.

Detailed semantics: [single capture](v5-scan.md),
[dual capture](v5-dual-capture.md), and
[Gauss analysis](../../deploy/gauss-analysis-v1/README.md).
