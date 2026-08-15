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
the required migration receipts, role privileges, and a write/fsync/unlink probe
inside the configured CAS temporary directory. Connections are scoped to each
operation and close on every exit; the filesystem adapter owns no background
resource.

The checked-in [example configuration](../../deploy/offline-analysis-v1/analysis.json)
uses the stable process configuration schema. Adapter names are exact selections,
not ambient discovery aliases. There is no production `PLUGIN` because no
detector/fitter pair has passed the locked scientific promotion gate.

## Safe station materialization

There is intentionally no runnable checked-in rehearsal plugin. Even a
refusal-only worker would claim and mutate the first matching durable job before
it learned that the science was unapproved. Pointing such a rehearsal at the
production DSN would damage queue state.

The checked config, schema, and non-installable `leo-offline-analysis@.service`
template live under `deploy/offline-analysis-v1/`. The template names the deliberately absent
operator module `leo_station.analysis_v1:PLUGIN` and has no `[Install]` section,
so this repository alone cannot start an analysis worker or claim a job.

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
