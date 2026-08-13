# ADR 0010: Durable dataset snapshot boundary

Status: accepted

## Context

ADR 0005 introduced an analysis-owned `DatasetSnapshot` that freezes explicit
split membership, scored/context roles, truth provenance, promotion diagnostics,
and the evaluated method. The earlier public model contract independently uses
`FeatureDatasetSnapshot`, whose ordered `FeatureSetRef` membership and digest are
already consumed by model fitters. The carve result was not serializable, while
the model snapshot did not retain split, role, or truth provenance.

Replacing or extending the published model contract would break its v0.1
compatibility. Persisting only the carve tuple would lose the exact feature
reference identity required by model analysis.

## Decision

Publish `org.leo-flow.dataset-snapshot-bundle/0.1` as an analysis-owned durable
envelope. It embeds an unchanged `FeatureDatasetSnapshot` and aligns each of its
ordered references one-to-one with immutable:

- correlation/split group and explicit train, validation, or locked-test split;
- scored-truth or context-only role;
- label source, target value, uncertainty, evidence digest, producer/time, and
  evaluated-method independence declaration;
- injection base-recording and injection-spec digests where applicable.

The existing model membership digest continues to close over feature-set ID,
analysis-run ID, and bundle digest. A second snapshot digest closes over that
digest plus selection provenance, evaluated method, rich member metadata, and
promotion result. Both exclude opaque, replaceable blob locators. A reference
pins the snapshot ID and both digests.

Construction accepts only the deterministic output of `carve_dataset`, an exact
set of candidates, and exact `FeatureSetRef` values. It rejects missing, extra,
or digest-substituted membership. There is no random split or IQ access.

The codec uses strict canonical JSON, rejects duplicate or unknown fields,
limits document size, and reconstructs the validating immutable types so both
digests are checked on every read. Dataset reader and publisher ports expose
only whole snapshots. The model composition root passes the embedded unchanged
`FeatureDatasetSnapshot` to existing fitters.

## Consequences

- Model implementations remain compatible and cannot access IQ or detector
  implementations through this boundary.
- A model orchestration layer can verify rich truth/split provenance before it
  exposes the embedded model snapshot.
- Relocating an identical feature blob changes serialized bytes but not either
  scientific identity digest.
- Locked-test access control and label sealing remain deployment/orchestration
  responsibilities; this schema makes the assignment immutable but does not
  authorize opening a sealed partition.
- Persistence adapters and a catalog migration are intentionally deferred; they
  must store the canonical bundle as one immutable object and publish its ref
  atomically rather than create per-member control-plane files.
