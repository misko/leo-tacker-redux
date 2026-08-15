# Three-part architecture simplification audit

Date: 2026-08-14

Capture, analysis, and dashboard are the only product processes. Storage, jobs,
contracts, adapters, and deployments support them rather than adding boundaries.

## Production composition map

Before:

```text
v5 deployment -> capture service -> engine -> storage ports
offline deployment -> duplicate cycle -> analysis services -> implementations
dashboard deployment -> dashboard service -> API/UI -> read-only query adapter

five blob consumers -> five private _BlobWriter copies -> blob implementation
recording submission -> private CredentialProvider copy -> systemd adapter
```

After:

```text
v5 deployment -> capture service -> engine -> storage ports
offline deployment -> compatible OfflineAnalysisCycle constructor
                   -> TypedAnalysisRouterCycle -> analysis services
dashboard deployment -> dashboard service -> API/UI -> read-only query adapter

five blob consumers -> storage.ports.BlobWriter -> blob implementation
recording submission -> bootstrap.SecretProvider -> systemd adapter
```

Dashboard remains read-only and cannot open IQ. Capture has no analysis/dashboard
implementation dependency. Analysis still receives exact capabilities by composition.

## Deleted duplication

- Deleted five identical private `_BlobWriter` protocol declarations from two
  storage publishers and three atomic analysis committers.
- Deleted the deployment-private `_LeaseExecutor`; the shared
  `AnalysisLeaseExecutor` now defines that capability.
- Deleted the second analysis claim/dispatch/lifecycle implementation;
  `OfflineAnalysisCycle` now preserves its API as a shared-router wrapper.
- Deleted the recording-submission `CredentialProvider` copy in favor of the
  existing bootstrap `SecretProvider`.

No module, public contract, schema, command, or entry point was deleted. Tests
prevent blob-writer redeclaration and prove reuse of the common router.

## Remaining debt and deferred cleanup

- PostgreSQL catalog adapters remain split across `adapters`, `analysis`, and
  `storage`; moving them is broad churn without behavior reduction.
- Atomic committers still import `Prepared*` service values and repeat small
  lease/result serialization helpers. Consolidating those changes application
  ownership and failure surfaces, so it was deferred.
- `FencedAnalysisCycle` and in-memory repositories are not production-plugin
  selections but remain supported convenience surfaces.
- The private dataset label model predates the public evidence taxonomy. It
  requires an explicit data migration, not deletion by architectural cleanup.
- Operator E2E modules intentionally remain bounded cross-boundary harnesses.

## Non-goals

This audit does not change contracts, schemas, SQL, scientific algorithms,
capture plans, radio drivers, ephemeris behavior, dashboard responses, or live
deployment configuration. It performs no network, radio, QNAP, or database I/O.
