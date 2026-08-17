# Deferred analysis throughput for the 936-slot R20/R21 campaign

Status: Release D implementation audited against a disposable PostgreSQL 16
database. This work made no live database, radio, service, or object-store
changes. Production arming still requires the immutable benchmark and promotion
gate below plus release freeze.

## Decision

Keep the reviewed capture-first boundary and exact per-batch campaign receipts,
but replace serial batch execution during `drain-analysis` with a bounded,
campaign-scoped worker topology:

1. Capture all 936 coordinated batches with no local analysis.
2. After the journal durably enters `analyzing`, hold the campaign lock and the
   host-wide pipeline-mode lock for the complete drain invocation.
3. Process one balanced 36-batch supercycle window at a time.
4. Idempotently submit the exact FeatureSet, waterfall, and Starlink v0.2 jobs
   for the window.
5. Drain each compute lane with at most eight isolated recording workers;
   drain projection lanes with at most four workers. Do not overlap the large
   compute lanes with each other.
6. Reconcile the 36 batches serially through the existing exact
   `CampaignAnalysisReceipt` validation and SQLite compare-and-swap journal.
7. Repeat until all 936 receipts exist, then durably enter `complete`.

The first production ceiling is eight compute workers, not the host's 24
logical CPUs. Promotion above eight requires the benchmark gate below. Every
child must set native math thread counts to one so eight Python workers do not
silently become many more native workers.

## Why the current service is too slow but restartable

The current `ExactCampaignAnalysis.analyze()` submits and completely drains one
batch before `DeferredCampaignCoordinator.analyze_next()` can select the next
one. The Starlink lane already runs the two recordings in that one batch in
parallel, but there is no parallelism across batches. FeatureSet and waterfall
work are also serialized at the batch boundary.

The 937 transition bound is correct: 936 batch receipts plus one phase-close
transition. It is not a throughput control and does not mean that only 937
database jobs are run. Each successful batch carries two FeatureSet jobs, two
waterfall jobs, two Starlink-suite jobs, their projection work, and dashboard
delivery.

The operator's 9-hour slice stops accepting a new batch when fewer than the
900-second per-batch deadline remains. It exits retryable status 75, and the
systemd unit restarts against the same definition and journal after 30 seconds.
Therefore the current service can converge if every batch remains below 900
seconds and no exact job parks, but it cannot meet a one-slice completion
expectation.

### Workload arithmetic

The balanced 936-slot definition produces 1,872 recordings:

| Rate | Batches | Recordings | Starlink v0.2 result |
| ---: | ---: | ---: | --- |
| 1.25 MS/s | 312 | 624 | terminal `not_evaluated`, clipped pilot band |
| 2.5 MS/s | 312 | 624 | eight methods per selected stream |
| 5 MS/s | 312 | 624 | eight methods per selected stream |
| **Total** | **936** | **1,872** | **1,248 eligible recordings** |

Using the v6 ordinary pair mean of 15.970 seconds and the observed v7-style
Starlink v0.2 pair wall time of approximately 52 seconds (the two recordings
already run in parallel):

| Topology | Ordinary estimate | Starlink estimate | Combined estimate |
| --- | ---: | ---: | ---: |
| Current batch-serial, observed mean | 4 h 09 m | 9 h 01 m | **13 h 10 m** |
| Current batch-serial, ordinary observed maximum | 6 h 20 m | 9 h 01 m | **15 h 21 m** |
| Four campaign workers, ideal scaling | 1 h 02 m | 4 h 30 m | 5 h 32 m |
| Six campaign workers, ideal scaling | 42 m | 3 h 00 m | 3 h 42 m |
| **Eight campaign workers, ideal scaling** | **32 m** | **2 h 15 m** | **2 h 47 m** |
| Eight workers plus 25% operating headroom | 40 m | 2 h 49 m | **3 h 29 m** |

These are capacity calculations, not benchmark results. The release gate must
replace the 52-second approximation with immutable measurements from the exact
release and representative real recordings.

## Required topology

### Window and lane order

A window is exactly 36 consecutive batches. It contains each of the nine
rate/dwell cells under each of the four radio edge geometries once. There are 26
windows in the eight-hour campaign. This bounds active work to 72 recordings
and 216 analysis jobs while keeping each scheduling window scientifically
balanced.

Run these barriers in order for each window:

| Lane | Maximum workers | Closure condition |
| --- | ---: | --- |
| Idempotent submission | 1 | exact 72 job IDs exist for each of the three job types |
| FeatureSet compute | 8 | all 72 exact jobs succeeded; none parked |
| Feature projection | 4 | all 72 exact outbox rows succeeded and dashboard recording analysis is complete |
| Waterfall compute | 8 | all 72 exact jobs succeeded; none parked |
| Waterfall projection | 4 | all 72 exact outbox rows succeeded and V3 recording views exist |
| Starlink v0.2 compute | 8 | all 72 exact jobs succeeded; clipped rows are `not_evaluated` |
| Starlink projection | 4 | all 72 exact outbox rows succeeded and V4 recording views exist |
| Receipt reconciliation | 1 | 36 exact campaign receipts committed by SQLite CAS |

Large compute lanes are deliberately not concurrent. This makes peak memory,
CAS read traffic, PostgreSQL connections, and CPU load predictable. Projection
work is inexpensive but remains a separate fenced boundary.

### Exact scoping

Do not let the production pool claim every job of a matching type from the
global queue. Add narrow, versioned claim ports that accept the finite exact
job/work ID set for the current 36-batch window. PostgreSQL implementations
must use `FOR UPDATE SKIP LOCKED`, an unguessable lease token, monotonically
increasing lease generation, and an indexed `job_id = ANY(...)` or equivalent
bounded predicate. Completion, retry, and park operations retain the current
token/generation/expiry fencing.

This is additive work. Do not change the published job or campaign contract in
place. Suggested new ports are:

- `CampaignAnalysisWindowPreparerV1.prepare(snapshots) -> PreparedWindowV1`
- `ScopedJobClaimPortV1.claim(job_ids, worker_instance_id, ttl)`
- `ScopedProjectionClaimPortV1.claim(work_ids, worker_instance_id, ttl)`
- `CampaignAnalysisWindowClosureV1.verify(prepared_window)`

`PreparedWindowV1` must bind the campaign definition digest, success-index
range, batch IDs, recording identity digests, exact job IDs, algorithm/config
refs, and expected output schemas. Its canonical digest is logged at every
restart. Submission remains idempotent and conflicts fail closed.

### Process and lock semantics

- The existing campaign lock serializes journal transitions for the complete
  invocation.
- Acquire `/home/mouse9911/.local/state/leo-flow/pipeline-mode.lock` once before
  preparing the first window and release it only after the invocation exits.
  This preserves analysis/capture exclusion across all child workers.
- Do not wrap reconciliation calls in a second independently opened mode lock;
  compose an unlocked exact analyzer beneath the drain-level lock. Keep the
  existing per-call lock for the separate `analyze-next` diagnostic command.
- Use the multiprocessing `spawn` context. Each child has its own PostgreSQL
  connection(s), CAS reader/committer, and unique bounded worker instance ID.
- Arm the parent-death signal before credentials or storage access. On any
  child protocol error, deadline, or nonzero exit, reap all siblings and fail
  the lane closed.
- Set `OPENBLAS_NUM_THREADS=1`, `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, and
  `NUMEXPR_NUM_THREADS=1` in the service and scrubbed child environment.
- Preserve the 900-second job lease until a separately tested heartbeat design
  exists. A hard-killed worker may delay restart progress by at most the lease
  expiry, but cannot publish through a stale generation.

## Restart, retry, and parked work

Every restart derives the first incomplete 36-batch window from the immutable
definition and durable journal. It resubmits the same deterministic identities,
then verifies PostgreSQL/CAS receipts before claiming work. A completed lane is
not recomputed. A partially completed lane claims only its unfinished exact
identities. Receipt reconciliation is idempotent through the existing journal
compare-and-swap transition.

| Condition | Required behavior |
| --- | --- |
| Slice deadline reached with unfinished retryable work | terminate/reap children, release locks, exit 75, restart same campaign |
| Process crash or host reboot | wait for fenced leases to expire, resume same window and identities |
| Transient job failure below attempt 3 | retain current 5-second delayed retry and exact identity |
| Invalid input or attempt 3 exhausted | park exact job, stop the window, exit non-restartable 4 with sanitized counts/IDs |
| Projection parked | stop the window and exit 4 |
| Receipt identity, digest, schema, or method-count mismatch | exit 4; never regenerate a fixture or replace the identity |
| Dashboard product missing after durable projection claims success | fail closure; do not advance the campaign receipt |

The global `capture_analysis_drain_ready()` function is not sufficient proof of
campaign completion because parked rows are terminal and intentionally do not
hold that gate closed. Production closure must be scoped to every exact identity
from the 936 captured batches and must reject parked work explicitly.

## Host budgets

The audited Gauss host has 24 logical CPUs, 122 GiB RAM (about 117 GiB currently
available), 8 GiB swap, and about 849 GiB free on the state/CAS filesystem. Raw
campaign acquisition is 32,614,400,000 bytes, so raw storage remains the
dominant known requirement. Derived-product size must be measured in the
benchmark; do not infer it only from two rendered waterfall examples.

Initial reviewed service limits:

| Resource | Initial bound | Rationale |
| --- | ---: | --- |
| Compute workers | 8 | one per performance core; leaves the remaining CPUs for PostgreSQL, dashboard, OS, and I/O |
| Projection workers | 4 | database/CAS work is small; avoids connection bursts |
| Worker subprocesses including parent | 16 maximum | accommodates reaping/transition overlap without an unbounded fork surface |
| `MemoryHigh` | 80 GiB | begins cgroup pressure before host exhaustion |
| `MemoryMax` | 96 GiB | leaves at least about 26 GiB nominally outside the unit |
| `TasksMax` | 64 | covers Python/native helper threads while enforcing the one-thread math policy |
| `LimitNOFILE` | 16,384 | bounded CAS and database descriptors across eight workers |
| Analysis slice | 6 h | above the 3 h 29 m modeled target; exit 75 remains resumable |
| systemd runtime | 6 h 10 m | leaves controlled operator cleanup time |

These are fail-closed Release D validation ceilings, not measured production
optima. Do not promote them for the main run until peak per-child RSS and scaling
are measured with the exact release. Storage admission before analysis must
include current free space, all remaining expected derived bytes at the measured
high percentile, temporary atomic-write amplification, and the existing 10 GiB
safety margin.

## Benchmark and promotion gate

Benchmark after capture, never concurrently with RF collection. Use the exact
immutable release, science manifest, migrations, CAS objects, and PostgreSQL
schema intended for production. The corpus must contain both radios and all
nine rate/dwell cells from a complete 36-batch canary. It therefore includes
clipped and eligible Starlink paths and both 2.5/5 MS/s probe sizes.

For worker counts 1, 2, 4, 6, 8, and 12, run at least three fresh, order-rotated
trials and record:

- per-lane and end-to-end wall time;
- job throughput and p50/p95/p99 job latency by rate/dwell/radio;
- peak and time-series parent/child RSS, cgroup memory, swap, CPU, load, and
  native thread counts;
- CAS read/write bytes, temporary peak bytes, and derived durable bytes;
- PostgreSQL active connections, claim latency, lock waits, deadlocks, WAL,
  retries, stale leases, and parked rows;
- exact job, result, projection, dashboard, and campaign receipt digests;
- restart trials during each compute lane and projection lane.

Promote the smallest worker count at the throughput knee, capped at eight for
the first main run, only when all of these hold:

1. All 72 recordings have exact FeatureSet, waterfall, and Starlink-suite
   terminal projections and all 36 campaign receipts reconcile.
2. Zero parked rows, stale commits, digest/schema conflicts, deadlocks, missing
   dashboard products, and leaked workers.
3. Every eligible Starlink product has eight methods per selected stream;
   every 1.25 MS/s product is `not_evaluated/clipped-pilot-band` with zero
   methods.
4. p99 runtime is less than one third of the lease TTL and less than one half of
   the per-window deadline.
5. Peak cgroup memory is below 80 GiB, swap remains zero, free CAS space remains
   above the derived admission reserve, and PostgreSQL retains reviewed headroom.
6. Scaling efficiency from the previous worker count remains at least 70%; if
   eight workers misses this gate, use six or four rather than forcing eight.
7. A killed worker, killed parent, and service restart each converge to the same
   exact receipts without duplicate semantic products.
8. The immutable benchmark receipt projects a conservative full-campaign drain
   below six hours, including at least 25% headroom.

## Production completion evidence

Declare the post-capture phase complete only when one immutable audit binds the
definition digest and proves all of the following:

| Evidence | Exact expected value |
| --- | ---: |
| Journal phase / analyzed count | `complete` / 936 |
| Successful campaign recordings | 1,872 exact recording IDs |
| FeatureSet jobs and projections | 1,872 succeeded / 1,872 succeeded |
| Waterfall jobs and projections | 1,872 succeeded / 1,872 succeeded |
| Starlink v0.2 jobs and projections | 1,872 succeeded / 1,872 succeeded |
| Starlink clipped products | 624 `not_evaluated`, zero methods |
| Starlink eligible products | 1,248 candidate products with exact eight-method closure per selected stream |
| Parked or retryable exact work | 0 |
| Dashboard batch rows | 936 batches, two successful recording rows each |
| Dashboard recording details | 1,872 FeatureSet results, 1,872 waterfalls, 1,872 V4 Starlink views |
| CAS integrity | every referenced object live, byte count and SHA-256 verified |

The dashboard and audit must continue to label Starlink output as candidate
evidence requiring whole-search calibration. This topology does not turn
uncalibrated scores into detections or a pilot count.

## Integration-owned changes and tests

The integration steward should implement the following together because they
cross application, PostgreSQL, deployment, service, and ADR ownership:

1. Add the versioned preparation/window/closure ports and campaign-scoped
   PostgreSQL claim functions in a new migration; retain existing public v2
   contracts unchanged.
2. Add the bounded spawn worker pool and drain-level mode-lock composition.
3. Teach `drain-analysis` to process/resume 36-batch windows, then reconcile
   through the existing coordinator and exact receipts.
4. Add worker/projection flags with strict bounds (`1..8`, `1..4`, window exactly
   36) and reject values that differ from the frozen deployment.
5. Update the analysis systemd unit with one-thread math settings, resource
   limits, a six-hour slice, and the exact release paths/digest.
6. Add component tests for scoped claims, lease generation/fencing, idempotent
   window replay, worker crash/reaping, delayed retry, park fail-closure,
   deadline exit 75, dashboard-missing closure, and no nested mode lock.
7. Add integration tests with fresh PostgreSQL for two simultaneous workers,
   exact-scope exclusion, expired lease recovery, all three analysis lanes,
   all three projection lanes, 36-batch restart/resume, 936-receipt closure,
   and systemd resource/exit policy.
8. Freeze the benchmark and production audit schemas, then include the exact
   receipts in the immutable release used to arm the main campaign.
