# ADR 0002: SigMF recording pair and contract v0.1 freeze

Status: accepted for Wave 1  
Date: 2026-08-13

## Decision

Use one contiguous little-endian CI16 SigMF data object plus one canonical JSON
metadata object as the v1 recording format. The two objects form one logical
`RecordingObjectRef` and become visible in one catalog transaction. No public
recording contract or reader can represent only one member.

Contract v0.1 is frozen for Wave 1. It includes explicit units and UTC
nanoseconds, typed scan/dwell activities, exact object and manifest hashes,
immutable feature/model/ephemeris references, covariance basis and units,
idempotent publication, and fenced job leases.

## Evidence

The host spike measured three one-GiB writes:

| Metric | SigMF | HDF5 |
|---|---:|---:|
| Median write throughput | 154.3 MiB/s | 152.6 MiB/s |
| 2 MiB slice | 0.101 ms | 1.224 ms |
| 95.37 MiB slice | 36.16 ms | 49.75 ms |
| Abrupt writer exit | Transparent, length-checkable partial | Consistency flag requiring quarantine/`h5clear` |

Both preserved exact CI16 samples. SigMF has the simpler dependency, recovery,
and hostile-input surface. Details and repeatable commands are in
`docs/spikes/recording-format.md`.

## Remaining hardware gates

The format is selected for implementation, not yet production-qualified. Before
Pi cutover it must pass narrow and 10 MS/s wide captures, one-hour schedule
soak, interruption/restart, exact independent reads, disk-pressure behavior,
and bounded QNAP reads. Peak wide input is 80 MB/s; local writes target at
least 160 MB/s for 2x headroom.

## Deferred contract surface

- Association and track payloads wait for the first model use case.
- Generated JSON Schema/decoders wait until a second process boundary consumes
  serialized contracts; dataclass and canonical-vector tests are authoritative
  in Wave 1.
- A codec change remains possible behind `RecordingWriter` and
  `RecordingObjectReader`, but requires a new format ID and conformance fixture.
