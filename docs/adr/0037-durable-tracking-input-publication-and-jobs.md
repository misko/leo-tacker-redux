# ADR 0037: Durable tracking-input publication and jobs

Status: accepted for implementation; tracking execution and promotion pending

## Decision

`TrackingInputSnapshot` is published CAS-first as one canonical bounded JSON
object and becomes visible only through an immutable PostgreSQL catalog row.
The row projects enough scientific identity to reject substitution: the exact
durable dataset identity, membership and snapshot digests, builder, selector,
provenance, entry count, and complete bundle metadata. The catalog is a live
object reference for retention and garbage collection. Publication and exact
reads fail closed on idempotency conflicts, object metadata disagreement,
noncanonical bytes, projection disagreement, and ambiguous identity.

The full `TrackingInputSnapshotRef` is an I/O reference and therefore contains
the current object locator. A separate `TrackingInputSnapshotIdentity` contains
the snapshot ID, snapshot and membership digests, and bundle digest, byte
count, media type, and format, but no locator. Durable scientific requests,
payloads, job identity, provenance, and model identity use only this
locator-independent identity. The repository resolves that identity to the
current full reference and verifies both before opening bytes. Moving exact CAS
bytes must not create a different scientific request or job.

Tracking uses a distinct strict `TrackingModelAnalysisRequest` and
`org.leo-flow.tracking-model-analysis-job/0.1` payload. It requires exactly one
tracking-input identity plus schema-bearing algorithm and configuration
artifacts. It does not add optional or default hardware, ephemeris,
calibration, or prediction fields because those are already closed inside the
tracking-input snapshot.

The existing `MODEL_ANALYSIS` queue capability remains the honest wider-model
boundary; a new job type or database migration would not create a new product
component. Descriptive model v0.1 jobs and tracking jobs retain different
payload schemas and preparers. A worker must install an explicit schema-aware
executor before it can claim and run the tracking payload; the existing
descriptive preparer continues to reject it.

## Consequences

Tracking execution receives one immutable scientific join instead of reopening
FeatureSets or consulting recording, hardware-link, ephemeris-link, provider,
clock, path, network, or raw-IQ capabilities. CAS relocation is operational
metadata, not a scientific input. Exact duplicate submission is restart-safe;
any changed dataset, measurement, covariance, calibration, ephemeris selection,
algorithm, or configuration produces a different identity.

The PostgreSQL migration must extend the exhaustive live-reference inventory,
install the live-object trigger, and preserve least-privilege roles. Tests must
cover relocation, substitution, noncanonical and oversized bytes, catalog
projection disagreement, idempotency and concurrency conflicts, retention/GC
races, strict payload decoding, and deterministic job identity.

