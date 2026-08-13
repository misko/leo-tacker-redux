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
