# ADR 0026: Exact model-job submission

## Status

Accepted.

## Context

ADR 0023 closes a frozen dataset over authoritative recording, hardware, and
ephemeris references, but callers still had to construct and enqueue a model
job manually. That left job identity and the point at which durable state could
be mutated unspecified.

## Decision

`ModelAnalysisSubmission` is the service-layer command. It names one complete
`DatasetSnapshotRef`, the exact ephemeris source, scope, policy artifact, and
`as_of_utc_ns`, plus immutable model-config and algorithm artifacts. The
submission service reads that exact dataset and calls `assemble_model_inputs`.
Only after all substitutions and missing authorities have failed closed does it
encode the existing `org.leo-flow.model-analysis-job/0.1` payload and enqueue it
as `MODEL_ANALYSIS`.

The job ID is the SHA-256 content identity of the strict payload schema and
canonical payload value. Retrying the same command therefore invokes the
durable repository's existing idempotent enqueue behavior. We do not add a
queue, migration, mutable alias, provider call, or capture dependency.

The existing job codec records the exact resolved ephemeris snapshot refs, not
the selection query that found them. The returned `SubmittedModelAnalysis`
retains the assembled identities for the invoking service client. Persisting the
selection query itself would require a separately versioned payload decision;
silently widening the strict v0.1 codec is outside this change.

## Consequences

- Dataset, feature, recording, hardware, or ephemeris substitution fails before
  enqueue.
- Identical strict commands map to one restart-safe durable job.
- Different config, algorithm, dataset, hardware, or ephemeris content maps to a
  different job identity.
- Submission performs no fitting, tracking, network access, capture, or
  dashboard projection.

## Tests

The in-memory rehearsal decodes the enqueued strict payload, proves duplicate
submission exposes one claimable job, and proves dataset and ephemeris-regime
substitutions do not call enqueue. A PostgreSQL integration test proves two
identical submissions create one physical job row through the production
repository adapter.
