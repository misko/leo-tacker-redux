# ADR 0003: Immutable ephemeris archive and temporal selection

Status: accepted for Wave 4
Date: 2026-08-13

## Decision

Treat provider retrieval, deterministic TLE normalization, temporal selection,
and scientific use as four separate operations.

Each successful retrieval stores three immutable content-addressed objects:

| Object | Format | Purpose |
|---|---|---|
| Raw response | `tle-raw-v1` | Exact provider evidence and offline replay |
| Normalized catalog | `tle-normalized-v1` | Provider-tagged, NORAD-sorted canonical JSON |
| Provenance | `ephemeris-provenance-v1` | Query label, times, checksums, parser, validation policy and attribution |

The catalog atomically publishes references to all three. Retrieval IDs are
idempotency keys; reuse with a different request or result is a conflict. CAS
paths remain private to the blob-store adapter.

The production HTTP adapters are outside analysis. Space-Track credentials are
passed as a non-serializable capability to an exact-host cookie-session adapter.
The Hugging Face adapter refuses credentials. Redirects and non-HTTPS/cross-host
requests are rejected. Unit tests inject transports and make no network calls.

## Selection semantics

- `AVAILABLE_THEN`: latest snapshot whose retrieval completed at or before the
  recording start. This prevents future information leaking into a historical
  or online experiment.
- `FIRST_AFTER`: earliest snapshot whose retrieval completed strictly after the
  recording finish, subject to the caller's `as_of` knowledge boundary.
- `BEST_EPHEMERIS`: remains fail-closed. “Best” has no frozen objective over
  element age, provider, lookahead, propagation residual, or tie-breaking.
  Adding it requires a new policy artifact and scientific validation; it must
  never silently alias either temporal rule.

The resulting `RecordingEphemerisInput` gives cross-recording analysis the
recording interval, selection policy/ref, exact normalized object and provenance
object. Analysis never queries a mutable “latest TLE” endpoint.

## Scheduling and failure semantics

UTC cadence slots deterministically derive retrieval and job IDs. Re-enqueueing
a slot is idempotent. Catch-up is bounded. Provider `Retry-After` is honored;
otherwise retries use capped exponential backoff. Authentication failures stop
for operator action, malformed/permanent responses do not retry, and transient
transport/5xx failures retry through fenced job leases.

## Consequences

Raw provider duplication does not multiply stored bytes because the CAS dedupes
content, while each retrieval retains separate provenance/catalog identity.
A persistent ephemeris catalog adapter and migration are still an integration
task; the in-memory catalog is the executable semantic reference.
