# ADR 0024: Durable detector evaluation publication

## Status

Accepted

## Context

Detector evaluation produces covariance matrices, split coverage, firing counts,
and truth-safe classification counts over a frozen dataset. Writing individual
windows or matrix cells as files would recreate the NFS file explosion that the
new architecture is intended to remove. A dashboard also needs bounded summary
queries without treating a projection as scientific ground truth.

## Decision

Publish exactly one canonical `DetectorEvaluationReport` JSON object to CAS.
Its SHA-256 digest is both the object address and the complete suffix of the
content-derived `eval_` identity. An explicit immutable `erun_` identity records
the evaluation execution. The blob is written first; one PostgreSQL transaction
then verifies the exact authoritative dataset, registers object metadata,
inserts the report row, and inserts one compact row per method/split.

PostgreSQL is the visibility point. Retries must reproduce the evaluation ID,
run ID, report digest, projection, object metadata, and idempotency key exactly.
Any reuse with different content is a conflict and the transaction exposes no
partial method summary.

Covariance, phi, shared-window, and shared-sample matrices remain in the single
canonical CAS report. They are not exploded into database rows without a
demonstrated query requirement. The dashboard projection returns bounded
method/split metrics, warnings, and the exact `ObjectRef` needed to retrieve the
canonical artifact. It never scans or constructs filesystem paths.

`leo_analysis` may select and append both tables but may not update or delete.
`leo_dashboard` may only select. `leo_capture` has no access. Dashboard queries
also assert a read-only transaction at runtime.

## Consequences

- Scientific reconstruction and covariance inspection use one verified report
  artifact rather than a lossy database reconstruction.
- Common dashboard pages avoid opening CAS and use a small normalized summary.
- Failed publication may leave an unreferenced CAS object for later garbage
  collection, but never a visible partial catalog report.
- New matrix-oriented dashboard requirements must justify and version a new
  projection instead of silently multiplying rows.
