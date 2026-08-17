# Offline analysis v1 composition

The offline analysis process has two and only two execution lanes:

| Lane | Input job | Exact inputs | Atomic output |
|---|---|---|---|
| independent recording | `recording_analysis` | recording object, algorithm, configuration, dependency refs | `FeatureSet` plus fenced job completion |
| cross-recording model | `model_analysis` | durable dataset, ordered FeatureSets, hardware snapshots, ephemeris snapshots, algorithm and configuration | `ModelSnapshot` plus fenced job completion |

`leo_flow.deployments.offline_analysis_v1` is the composition boundary. It
claims only those two typed job kinds. It does not import capture, radio,
provider HTTP, TLE retrieval, orbit propagation, or dashboard code. Ephemeris
retrieval and recording-to-ephemeris backfill remain separately deployed job
capabilities.

## Scientific plugin seam

There is deliberately no repository-wide default detector or fitter. A station
deployment constructs `StationScientificFactories` and calls
`build_station_plugin(...)` with:

- durable job, recording, dataset, FeatureSet, ephemeris, hardware, CAS and
  catalog adapters;
- `ExactRecordingAnalyzerRegistry`, keyed by the complete algorithm and config
  `ArtifactRef` values carried in each recording job;
- `ExactModelFitterRegistry`, keyed by the complete algorithm and model-config
  `ArtifactRef` values carried in each model job;
- the two atomic PostgreSQL/CAS committers.

An unregistered exact pair fails the leased job. There is no `latest`, directory
scan, filename convention, or fallback algorithm. The model lane receives only
the durable dataset reference named by its payload and opens only the exact
ordered artifact references in that snapshot.

The builder supplies the durable PostgreSQL/CAS adapters and the two atomic
committers. It requires an absolute CAS root, one systemd `catalog-dsn`
credential, and complete non-empty exact registries for both lanes. The DSN
login must be a member of `leo_analysis` and able to read migration receipts;
all operational queries execute after `SET ROLE leo_analysis`. Readiness proves
the required migration receipts through
`0033_registered_analysis_during_capture.sql` (including the waterfall and
Starlink candidate catalogs and dashboard projection migrations), role privileges
(including function-only access to both durable projection outboxes), and a
write/fsync/unlink probe inside the configured CAS
temporary directory. Connections are scoped to each operation and close on
every exit; the filesystem adapter owns no background resource.

The checked-in [example configuration](../../deploy/offline-analysis-v1/analysis.json)
uses the stable process configuration schema. Adapter names are exact selections,
not ambient discovery aliases. There is no production `PLUGIN` because no
detector/fitter pair has passed the locked scientific promotion gate.

## Safe station materialization

There is no repository-wide fallback plugin. A worker can claim work only when
an explicit station package exports complete exact scientific registries. The
checked Gauss development package is `leo_station.analysis_v1`; its matching
service config, immutable science approval, post-capture submission command, and
bounded analysis/projection commands live under `deploy/gauss-analysis-v1/`.
They pin the current development algorithms and Python 3.11.16 environment and are
not a production scientific promotion.

The Gauss package also accepts one explicitly supplied canonical public
`CaptureBatchSnapshot`. Its closed-batch submission service requires both
attempts terminal, verifies each successful `PublishedRecordingRef` against the
public catalog, and enqueues only the pinned per-recording jobs in canonical
attempt order. It retains one successful recording after peer failure and
reports paired eligibility without creating or implying paired science. The
submission and processing operators hold Gauss's shared capture/analysis mode
lock before credential, database, or CAS use. The bounded `drain-batch` command
reports whether its final immediate claims found no work; it does not call
delayed or parked work globally quiescent. It never scans capture SQLite, the
CAS, or filenames for work.
The Gauss deployment first republishes the deterministic initial batch
dashboard view, so exact replay repairs a prior projection outage and projection
failure prevents recording-job enqueue. This dashboard coupling remains in the
station deployment; the public closed-batch submission service is
dashboard-agnostic.

Gauss additionally exposes a versioned, bounded waterfall lane. It is not part
of capture completion and is not claimed by the generic two-lane offline
service router: `submit-waterfall` selects one exact published recording,
`process-waterfall-one` produces and atomically catalogs one bounded bundle,
and `project-waterfall-one` resolves that exact public reference into the
dashboard projection. Each step is fenced, retryable or parkable, and protected
by the same shared capture/analysis mode lock. The dashboard never reads CAS.

The checked Gauss package also exposes one authoritative Starlink candidate
lane: `submit-starlink`, `process-starlink-one`, and
`project-starlink-one`. Submission opens only the exact catalog-selected
recording, accepts the approved 2.5 or 5 MS/s full-pilot scan geometry, selects
the exact rate-specific search config and probe bound, and derives each
lower/upper Qin template pair from immutable segment tags and actual sample
rate. Both profiles cover the same 8 ms and 583-cell bank; 5 MS/s is valid
because its sampled 5 MHz band fully contains the 1.875 MHz pilot allocation.
The 1.25 MS/s arm is short by 625 kHz and remains ineligible. The checked
scientific approval's canonical digest is
`sha256:99a44bd3c31affe541d4891bb16d6a42ed634cdbe542da59caefc0e491518003`.
Processing is post-capture and local to Gauss. Catalog publication, the
projection outbox, and dashboard publication are independently lease-fenced;
the dashboard receives a bounded semantic projection and no CAS locator.
Candidate scores are not detections and never receive a detection count without
an exact matching approved whole-search calibration artifact.

The next rollout requires the exact 33-file migration chain through
`0033_registered_analysis_during_capture.sql` as one reviewed maintenance action.
Verify migration 0030's frozen SHA-256
(`005d5408a24d2d507fe6ebaa3d4b8b86fe46b92a0a498f1f1151cbe2bc8e4cab`),
apply it once, run readiness, and only then start the staged analysis service.
The 1.25 MS/s records remain explicitly `Not evaluated`; do not coerce them
into a full-pilot config. Continuous 2.5 or 5 MS/s captures use the approved
suite profile. Replay is content-addressed and idempotent, so interruption does
not require catalog or CAS discovery.

The generic checked config, schema, and non-installable
`leo-offline-analysis@.service` template live under
`deploy/offline-analysis-v1/`. The template still has no `[Install]` section,
so installing the Python package alone cannot enable or start a worker.

Production installation still requires exactly one operator-owned, importable
module, for example `leo_station.analysis_v1:PLUGIN`, containing:

1. the complete algorithm and config `ArtifactRef` for every approved recording
   analyzer and its constructed factory;
2. the complete algorithm and model-config `ArtifactRef` for every approved
   model fitter and its dataset-scoped builder;
3. the authoritative shared CAS root used by capture publication; and
4. a call to `build_station_plugin`, exported under the exact attribute named by
   the station systemd unit.

The materialized unit must grant the dynamic user access to exactly that shared
CAS root (for example with a station-owned group/ACL and `ReadWritePaths`) and
must retain systemd credential loading. Promotion installs the recorded station
artifact only after the held-out scientific gate; configuration never selects
an algorithm by a mutable database row.

Exact recording jobs are created by the separate
[analysis-side submission command](recording-analysis-submission.md). That
command reads the durable published recording catalog and writes the durable
PostgreSQL queue; capture has no analysis-submission capability. PostgreSQL does
not transport IQ bytes, so the shared or replicated CAS binding described there
must exist before an off-host worker can consume a submitted recording.

## Assembly and rehearsal

Low-level tests may construct the service with
`build_offline_analysis_service(config, components, lease_ttl_s=...)`. A real
station process runs the common service CLI with its explicit plugin module.
For the packaged operations target, copy `analysis.json` to the template's exact
`/etc/leo-flow/analysis-worker-1.json` path and give every additional `%i`
instance a distinct config and `runtime.instance_id`.

The deterministic component rehearsal covers exact routing, restart-safe
idempotency, exclusion of ephemeris jobs, unknown algorithm failure, and model
failure fencing without radio or network access. PostgreSQL integration tests
separately prove that FeatureSet/ModelSnapshot visibility and job completion
share one transaction.
