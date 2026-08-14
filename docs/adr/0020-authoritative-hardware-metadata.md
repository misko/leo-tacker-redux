# 0020: Authoritative effective-dated hardware metadata

Status: accepted

## Decision

Hardware metadata is published as one bounded, canonical JSON snapshot. Its SHA-256
digest is both the immutable snapshot reference and the content-addressed bundle
identity. Publication writes the bundle to CAS before atomically inserting its
immutable PostgreSQL catalog projection.

The catalog normalizes the ordered radio membership and ordered receiver-chain
rows. Every receiver row belongs to a radio in the same snapshot. A receiver may
have successive LNB, polarization, and cable assignments, but its half-open
effective intervals may not overlap. Readers require the exact snapshot ID and
digest, verify the complete object reference and bytes, decode the strict schema,
and compare the decoded value with the catalog projection.

Analysis owns append publication. Capture and dashboard have read-only access.
Capture and model-analysis consumers depend only on `HardwareMetadataReader`; they
do not import PostgreSQL, CAS, or publication adapters.

## Consequences

- A recording can retain the exact hardware snapshot ID already present in its
  manifest while downstream jobs carry the exact ID/digest reference.
- LNB swaps and wiring changes are historical facts rather than mutable current
  state.
- Retry keys are stable and conflicting identity, bytes, or projection data fail
  closed.
- CAS objects left by a failed catalog transaction are safe unreferenced objects
  for a later privileged garbage collector.

## Verification

Golden codec tests cover deterministic round trips and malformed input. Repository
tests cover CAS-first publication, exact reads, idempotency, and projection
disagreement. PostgreSQL tests cover atomic publication, normalized membership,
concurrent retries, immutable role grants, and transaction rollback.
