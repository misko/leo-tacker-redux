# ADR 0008: Preserve verified radio-time gaps without inventing continuity

Status: accepted for Wave 6.

## Context

V5 can return individually authenticated metadata refills when the transport
cannot carry every source frame. The stored IQ bytes remain useful for
post-processing, but byte adjacency at a lost-frame boundary is not radio-time
adjacency. Discarding the entire dwell wastes valid observations; calling it
continuous corrupts timing, spectra, and labels.

## Decision

Continuity has three states: `verified_contiguous`, `verified_gapped`, and
`unverified`. The old Python names `VERIFIED` and `REQUIRE_VERIFIED` remain
aliases for verified-contiguous behavior. `REQUIRE_CONTIGUOUS` fails closed on
any source gap. Only the explicit `ALLOW_VERIFIED_GAPPED` capture policy may
publish a metadata-verified discontinuous segment. Failure flags, overflow,
sequence regression or overlap, stream changes, and stored-IQ offset errors
remain fatal under every verified policy.

Each verified gap records the adjacent refill indexes, exact stored-sample
boundary, missing radio-sample half-open extent, missing sample count, and
missing buffer count. These values are derived from the refill evidence and
validated on both write and read; callers cannot supply a contradictory gap
table. Every stored sample in a verified segment must be covered by one refill.

The recording metadata namespace advances from 1.1 to 1.2. The raw paired
little-endian CI16 object is unchanged. Canonical refill and gap tables stay in
the separately hashed metadata object. Readers accept 1.0, 1.1, and 1.2:

- 1.0 has no continuity record;
- 1.1 `verified` decodes as `verified_contiguous` with no gaps;
- 1.2 carries the explicit status and mandatory canonical gap table.

Recording views expose proven contiguous RF spans and safe fixed-size window
iteration. A safe window is wholly contained in one span. No API to cross a
gap is provided. The existing quality/PSD analyzer splits descriptive quality
summaries by span and obtains PSD starts independently inside each span.
Legacy/unverified analysis behavior is retained for compatibility but conveys
no continuity proof; dataset promotion must use continuity facts explicitly.

## Consequences

- Slow transports can preserve scientifically useful V5 IQ atomically without
  representing missing radio time as samples.
- Algorithms see stored-sample coordinates while gap records retain the source
  sequence extent needed for timing-aware work.
- Windows near either side of a gap may be omitted when a span is shorter than
  the configured window. This is correct missingness, not a negative firing.
- No database migration is required: the recording object reference and CAS
  publication contract are unchanged. Existing object bytes are immutable.
- Consumers that compare the literal old wire status `verified` must add the
  1.2 values before reading new objects. The Redux reader handles both.

## Verification

Independent malformed metadata fixtures cover sequence regression, stream
change, stored-offset error, overflow flags, and contradictory gap extents.
Contract and codec tests cover single and multiple gaps, exact round trips,
legacy 1.1 decoding, and safe span/window boundaries. Capture tests prove the
explicit gapped policy publishes while the contiguous policy aborts atomically.

