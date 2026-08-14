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
deployment constructs `OfflineAnalysisComponents` and supplies:

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

The checked-in [example configuration](../../config/offline-analysis-v1.example.json)
uses the stable process configuration schema. Its adapter names are selections
that a station-owned deployment plugin must register; they are not ambient
discovery aliases. The repository does not yet export a ready-to-run global
plugin because production detector and fitter factories have not been approved.

## Assembly and rehearsal

Construct the service with `build_offline_analysis_service(config, components,
lease_ttl_s=...)`, then run it through the common `ServiceLoop`. Preflight must
verify database connectivity, migrations/roles, and CAS accessibility before
readiness. Shutdown must close only resources owned by the injected deployment.

The deterministic component rehearsal covers exact routing, restart-safe
idempotency, exclusion of ephemeris jobs, unknown algorithm failure, and model
failure fencing without radio or network access. PostgreSQL integration tests
separately prove that FeatureSet/ModelSnapshot visibility and job completion
share one transaction.
