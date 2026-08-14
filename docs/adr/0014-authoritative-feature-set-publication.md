# ADR 0014: Authoritative FeatureSet publication

Status: accepted

## Context

Independent-recording analysis produces one immutable `FeatureSetBundle`, but
the public publisher previously accepted only an object reference and a small
projection. It could not derive the bundle's analysis-run identity or verify
that the bytes, analysis request, recording input, and provenance formed one
closed artifact. Dataset snapshots therefore could pin feature references but
could not prove that those references had been published by this system.

## Decision

The analysis application publishes a complete `RecordingAnalysisRequest` and
canonical `FeatureSetBundle` through one `FeatureSetPublisher` capability. The
durable repository validates their recording, configuration, dependency, and
provenance closure before writing anything. Projection values are private and
always derived from the bundle; callers cannot supply them.

One bounded canonical JSON object is uploaded to content-addressed storage.
The PostgreSQL transaction then verifies the exact source recording, registers
the object metadata, and inserts one immutable `feature_set` row. That row is
the only visibility point. A database failure may leave an unreachable CAS
object for privileged garbage collection, but cannot expose a partial feature
publication.

Feature ID, bundle digest, and idempotency key are immutable identities. An
exact retry returns the existing reference; any inconsistent reuse fails
closed. Readers select the exact `FeatureSetRef` in a read-only transaction,
verify CAS metadata and bytes, strictly decode the canonical object, and compare
the complete catalog projection. PostgreSQL never reconstructs a FeatureSet in
place of its object.

Dataset members now have a validated foreign key to the feature ID, analysis
run, and bundle digest. Dataset publication cannot manufacture feature-object
ownership. Full object metadata remains verified by the exact feature reader.

`leo_analysis` may append and read feature publications. `leo_dashboard` may
read the projection. Capture has no feature access, and no runtime role may
update or delete published rows. Dashboard projections remain separately
retryable derivatives and are not part of the publication transaction.

## Consequences

- One analysis result creates one canonical object and one catalog row.
- Model and dataset analysis can resolve exact, independently published inputs.
- At-least-once workers can safely derive their publication key from the job ID.
- Job payload decoding, scheduling, detector calibration, and model publication
  remain separate concerns.
