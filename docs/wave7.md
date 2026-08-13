# Wave 7: durable composition without boundary leakage

Date: 2026-08-13

Wave 7 turns the green component cores into honest process and persistence
boundaries. It does not add a workflow engine, teach capture about analysis, or
promote an uncalibrated detector.

## Parallel ownership

| Workstream | Owns | Consumes | Must not acquire |
|---|---|---|---|
| Dataset snapshot | Frozen membership, split/truth provenance, immutable codec and reader/publisher ports | Published `FeatureSetRef` identities | IQ readers, detectors, fitted models |
| Dashboard PostgreSQL | Read-only implementations of `DashboardQueryPort` | Catalog/projection rows | Writers, job leases, capture or analysis implementations |
| Service bootstrap | Configuration, exact adapter resolution, secret-provider references, lifecycle invocation | Process-specific factories and public service protocols | Ambient aliases or capabilities belonging to another process |
| Integration | Contract reconciliation, cross-boundary tests, migrations/CI review | The three component results | Component-private implementation access |

## Strict seams

1. Dataset membership is identified by canonical content, not a query that can
   change later. Split, scored/context role, and truth provenance travel with
   the immutable snapshot. Model fitting opens that exact snapshot through a
   reader port; it cannot silently reconstruct membership from the catalog.
2. Dashboard receives a query-only database capability. Time intervals are
   half-open, activity kinds are declared capture facts, pagination is bounded
   and query-bound, and directory contents are never authoritative.
3. Bootstrap resolution starts from one versioned service configuration and
   one exact deployment manifest. Factories are registered under exact names.
   Secret values are resolved only through named providers and are never
   accepted inline. Resolution completes before a process begins side effects.
4. Capture, analysis, and dashboard have disjoint registries. A configuration
   cannot obtain a factory or secret outside its process capability set.

## Acceptance tests

| Boundary | Required evidence |
|---|---|
| Dataset | Deterministic round trip; tamper rejection; injection/base grouping; explicit time-ordered partitions; independent truth gates; no IQ/detector import |
| Dashboard | PostgreSQL query parity with the in-memory contract; exact per-radio scan/dwell counts; snapshot-stable keyset pages; SQL-injection resistance; read-only transaction behavior |
| Bootstrap | Unknown/mismatched references fail before startup; inline secrets rejected; process capability isolation; deterministic one-shot/continuous exits; bounded clean shutdown |
| Integration | Published features freeze into a dataset consumed by model analysis; projections remain read-only; retries preserve identity; full dependency-lane and V5 runtime CI remain green |

## Explicit deferrals

- No detector threshold is promoted until a qualifying independently labelled
  dataset exists.
- No embedded optimization is started in this wave.
- No live radio, Space-Track, or Hugging Face call is part of ordinary tests.
- LNB/satellite physical fitting and tracking remain the next scientific wave;
  Wave 7 supplies the frozen inputs and runnable boundaries they require.
