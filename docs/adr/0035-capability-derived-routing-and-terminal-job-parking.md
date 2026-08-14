# ADR 0035: Capability-derived routing and terminal job parking

Status: accepted

## Decision

An analysis worker claims exactly the `JobType` keys present in its immutable
executor mapping. It does not claim a repository-wide list and does not install
placeholder executors that mutate unsupported work into failure. Adding a
capability therefore requires an actual executor in the station composition.

Retryable failure remains the existing fenced `fail` transition with an exact
future availability time. A distinct fenced `park` transition represents a
permanent or operator-action outcome. Parked jobs are terminal and excluded
from claims; they retain only a bounded reason code and parking time. This
repository deliberately provides no runtime requeue operation.

Ordinary analysis credentials no longer have direct `INSERT` or `UPDATE` on
the job table. Fixed, security-definer functions with a pinned search path own
enqueue, claim, heartbeat, lease locking, completion, retry, and parking. The
lease-lock function holds the row lock inside the caller's transaction so
FeatureSet, ModelSnapshot, ephemeris snapshot, and ephemeris-link publication
remain atomic with job completion.

## Consequences

Retrieval-only analysis profiles cannot consume recording, model, or link jobs.
Authentication and invalid-provider failures can stop without a hot loop, while
transient failures retain ordinary retry behavior. A future operator requeue
must be introduced as a separately authenticated maintenance decision with its
own audit evidence; direct SQL mutation is not that interface.

The in-memory and PostgreSQL repositories must have matching parking and fence
semantics. Role tests and all four atomic-committer tests are required whenever
the job transition functions change.
