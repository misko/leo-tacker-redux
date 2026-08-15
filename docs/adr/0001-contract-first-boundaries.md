# ADR 0001: Contract-first component boundaries

Status: accepted for Wave 0  
Date: 2026-08-13

## Decision

The repository has three applications—capture, analysis, and dashboard—and two
analysis domains: independent recording analysis and cross-recording model
analysis. Components communicate only through versioned value contracts and
narrow capability ports.

Public contracts are frozen before parallel production work begins. A later
contract change requires an ADR that names affected producers and consumers,
compatibility impact, migration behavior, and changed golden fixtures.

## Compatibility gates

Each transition carries the smallest public authority needed by its consumer:

| Transition | Public value | Compatibility and identity gate |
|---|---|---|
| Capture completion → publication | `CompletedLocalRecording` → `PublishedRecordingRef` | Recording manifest is exactly v0.1; its canonical digest and the distinct data/metadata blob identities must agree; the selected reader also requires the exact v1 media/format IDs and verified blob metadata. |
| Published recording → independent analysis | `RecordingAnalysisRequest` | Request is exactly v0.1, pins the complete `RecordingObjectRef`, and selects exactly the v0.1 `FeatureSetBundle` output. |
| Independent analysis → FeatureSet publication | `FeatureSetBundle` → `FeatureSetRef` | Canonical decoder rejects unknown fields and unsupported versions; publication closes bundle IDs and provenance over the exact recording identity. |
| Frozen FeatureSets → aggregate/model publication | `ModelAnalysisRequest` → `ModelSnapshotBundle` → `ModelSnapshotRef` | Canonical decoder rejects unknown fields and unsupported versions; publication closes membership, hardware, ephemeris, configuration, and dependency identities. |
| Published authorities → dashboard | projection commands → `DashboardQueryPort` DTOs | Writers validate exact published refs before reduction; the read model exposes no blob locator, provenance, or private persistence model, and versioned cursors fail closed. |

Minor versions are not accepted implicitly by the currently frozen v0.1
artifact constructors and codecs. A reader may use `SchemaVersion.can_read`
only when a later ADR explicitly approves compatible-minor behavior for that
specific serialized boundary. Unknown fields are rejected instead of silently
discarded, so adding a field also requires an intentional version and migration
decision.

## Enforced dependency direction

```text
capture ----------------------> contracts <---------------- dashboard
   |                               ^                              ^
   v                               |                              |
storage adapters <---------- composition root ---------- read repository
                                   |
                    +--------------+--------------+
                    v                             v
          recording analysis              model analysis
                                                  ^
                                                  |
                                              ephemeris
```

- Capture has no detector, TLE, model, or dashboard capability.
- Recording analysis receives one recording and cannot enumerate others.
- Model analysis receives frozen feature-set references and cannot open raw IQ.
- Dashboard has read-only query and diagnostic capabilities.
- Concrete adapters are wired only by the composition root.
- PostgreSQL projections are rebuildable; immutable bundles are scientific
  authorities.

## Consequences

Agents can build against golden contracts and in-memory fakes without importing
one another's unfinished implementation. Some integration convenience is
deliberately rejected to keep the boundaries testable and durable.
