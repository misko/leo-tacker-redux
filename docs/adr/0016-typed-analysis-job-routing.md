# ADR 0016: Typed analysis job routing

Status: accepted

## Context

Analysis now has capability-specific workers whose committers atomically fence
artifact visibility with job completion. Combining their payload decoding,
scientific preparation, provider retry policy, and publication rules in one
processor would recreate a god service and weaken those independently tested
transaction boundaries.

## Decision

The analysis process uses a small typed router cycle. It claims the complete
declared `JobType` tuple and dispatches each lease through an exact enum-to-
executor map. Every executor exposes only `execute(JobLease)` and retains
ownership of payload validation, preparation, retry/failure policy, artifact
publication, and terminal lease mutation. The router never publishes, completes,
or refails a lease.

The existing recording-analysis and ephemeris-retrieval workers implement this
shape directly. Model analysis uses the same protocol without sharing their
readers or committers. `ephemeris_link_backfill` is deliberately represented by
an unavailable executor: it fences the claimed lease into `failed` with a
bounded reason code and a year-9999 retry time, then raises. This prevents a hot
retry loop and requires an explicit operator requeue when that capability is
implemented.

## Consequences

- Adding an executor does not add scientific or storage dependencies to routing.
- A component fault retains the component's already-tested atomicity semantics.
- All four job types are visible in routing; unsupported backfill cannot be
  silently skipped or accidentally handled as another payload.
- Deployment assembly and the final durable model vertical slice remain separate
  integration work and do not change this routing contract.
