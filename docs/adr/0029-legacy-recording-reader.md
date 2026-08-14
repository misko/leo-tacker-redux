# ADR 0029: Explicit, read-in-place legacy recording registration

## Status

Accepted for the Phase 3 historical-data boundary.

## Decision

Historical IQ remains in its existing read-only archive. `LegacyRecordingReader`
accepts only an operator-configured map from opaque storage-root IDs to absolute
local roots and an explicit set of immutable registrations. It never discovers
directories, chooses a latest file, copies IQ, imports `leo-tracker`, or derives
identity from a mutable mount path.

Each registration binds an exact source-manifest size and SHA-256, an ordered set
of exact payload sizes and SHA-256s, and a factual redux `RecordingManifest`.
The logical data-object digest remains an ordinary content identity: SHA-256 of
the concatenated selected dwell bytes, with byte count equal to their sum. A
separate canonical selected-chunk index digest is provenance only. The source
manifest is the metadata object. Opening verifies every complete file and the
logical concatenated digest before exposing the existing
`RecordingObjectReader`/`RecordingView` capabilities. Component-wise
`O_NOFOLLOW` opens confine all paths and reject symlinks. Open descriptors are
checked for mutation around reads.

The first adapter recognizes only a complete `leo-tracker.beacon-iq/v1` source
with CI16 little-endian `sample,receiver,component` layout, two receivers, one
factual redux dwell segment, and contiguous chunk indexes. Reads may cross
physical chunk boundaries without joining or rewriting files. If the source
also declares a pre-dwell `survey_iq`, registration must explicitly identify
that exact file as omitted from the logical dwell. Its full bytes are verified
at open, but it is not included in the dwell data-object digest or exposed as a
dwell segment. This makes the subset boundary visible and preserves the
`ObjectRef` meaning: its byte count and digest identify exactly the selected
ordered chunk stream. Legacy chunks do not establish hardware RF continuity,
so continuity-dependent APIs fail closed.

## Why registration is required

The legacy source does not truthfully contain every required redux fact: among
them are the capture plan identity, authoritative hardware snapshot, verified
station/receiver-chain identities, radio serial, and monotonic start. Supplying
plausible placeholders would turn path conventions into scientific metadata.
The caller must obtain those facts independently and register them explicitly;
the adapter cross-checks every fact that the legacy source does declare.

Current development-corpus dwell manifests contain a pre-dwell survey at a
different rate and with a tuning axis. It is therefore an explicitly verified
omission, never silently treated as dwell data. Exposing the survey itself,
cropped `evidence_clips`, interrupted sources, multi-segment mappings, other
dtypes/layouts, and sources lacking exact per-file hashes remain unsupported
until a truthful versioned registration format is approved.

## Compatibility and testing

No public recording contract changes and no legacy runtime dependency are
introduced. Tests use independent format fixtures and cover cross-chunk reads,
unknown identities, substitution/corruption, declared-size mismatches,
traversal, symlinks, surveys, non-contiguous chunks, and metadata mismatch.
