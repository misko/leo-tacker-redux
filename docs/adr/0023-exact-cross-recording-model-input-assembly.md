# ADR 0023: Exact cross-recording model-input assembly

## Status

Accepted.

## Context

A frozen dataset pins feature-set objects, partitions, roles, and truth evidence,
but `ModelAnalysisRequest` also needs the hardware and ephemeris snapshots used
by every underlying recording. Choosing `latest` metadata while starting a model
run would make the same dataset produce different scientific inputs. A recording
may legitimately have several ephemeris links for different providers, scopes,
policies, or selection cutoffs, so a recording-only lookup is also ambiguous.

## Decision

Analysis assembles model inputs from one exact `DatasetSnapshotBundle` and its
full `DatasetSnapshotRef`. It opens each frozen feature object to recover its
recording ID and input recording identity digest, then verifies that digest
against the authoritative recording, hardware-link, and ephemeris-link
catalogs.

Ephemeris lookup is keyed by recording ID plus exact source, scope, selection
policy, policy artifact, and `as_of_utc_ns`. The as-of value is supplied by the
caller and must not exceed the dataset selection cutoff. It is never inferred
from the available link rows. Hardware and ephemeris snapshot references retain
first-recording order and are de-duplicated only when their complete immutable
references agree. Reusing a snapshot ID with different digests is an error.

The assembler constructs the existing `ModelAnalysisRequest`; it does not add a
new persistence format or database migration. `RecordingEphemerisLink` becomes
a public immutable contract so custom and PostgreSQL catalogs are subject to the
same link-ID and canonical-digest checks.

## Consequences

- Missing links, substituted datasets/features/recordings, identity conflicts,
  selection-regime mismatches, and duplicate snapshot-ID conflicts fail before
  model fitting.
- Multiple ephemeris links for one recording are safe because no API offers an
  arbitrary recording-only `fetchone` path.
- Reproducing a model run requires retaining the rich dataset ref and the exact
  ephemeris selection requirement alongside the normal request/job payload.
- This boundary performs no capture, TLE retrieval, orbit propagation, tracking,
  dashboard projection, or mutable `latest` lookup.

## Tests

Unit tests cover exact ordering/de-duplication, missing links, all identity and
regime checks, cutoff leakage, conflicting snapshot references, and dataset
substitution. PostgreSQL integration creates two ephemeris links for one
recording and proves the exact reader returns only the fully keyed link.
