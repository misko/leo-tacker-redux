# ADR 0015: Atomic fenced recording-analysis publication

Status: accepted

## Context

ADR 0014 makes a FeatureSet catalog row the visibility point for one immutable
analysis result. A generic worker that publishes that row and then separately
marks its at-least-once job complete still has an ambiguous crash window: a
retry can observe a published artifact while the job remains leased or failed.
It can also allow an expired worker to publish after a replacement lease has
already been issued.

## Decision

Recording analysis has a narrow typed worker rather than adding more routing to
the generic analysis cycle. It claims only `recording_analysis`, rejects every
other job type, strictly decodes one versioned `RecordingAnalysisRequest`
payload, opens the exact recording, and prepares the `FeatureSetBundle` before
starting a publication transaction. Model and ephemeris work remain outside
this worker.

The canonical FeatureSet object is uploaded to CAS before PostgreSQL work. The
committer then locks and verifies the live job token, generation, type, state,
and database-clock expiry. In that same transaction it verifies the exact
source recording, registers the FeatureSet object, inserts or exactly retries
the authoritative FeatureSet row, and changes the job to `succeeded` with an
`ArtifactRef` for that bundle. A stale lease or any fault rolls the entire
database transaction back, exposing neither a FeatureSet row nor a completed
job. A pre-transaction failure can leave only an unreachable content-addressed
object for privileged garbage collection.

The idempotency identities are derived from the job ID. The database transaction
is the source of fencing truth; a process clock is never used to decide whether
publication is still authorized. Preparation performs no database mutation and
can be retried under a replacement generation.

## Consequences

- Feature visibility and job success are one atomic database decision.
- Expired workers cannot publish after a newer generation is leased.
- The worker has no model-analysis or ephemeris routing behavior.
- CAS and PostgreSQL still do not pretend to share a distributed transaction;
  orphan object collection remains an explicit maintenance responsibility.
