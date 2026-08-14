# ADR 0016: Authoritative ModelSnapshot publication and release history

Status: accepted

## Context

Model fitting produces one immutable `ModelSnapshotBundle`, but the original
publisher accepted a caller-supplied object reference and parameter-count
projection. It could not prove that those values described the same bundle or
that the model's provenance closed over an authoritative dataset. The existing
in-memory staging helper made this missing authority visible but did not solve
it durably.

## Decision

`ModelPublisher` accepts the complete `ModelAnalysisRequest` and canonical
`ModelSnapshotBundle`. The repository derives all object metadata and catalog
projection values. Before writing, it validates the request's dataset
membership, configuration, hardware and ephemeris digests, exact dependency
ordering, parameter identities, and the bundle's provenance envelope.

One bounded canonical JSON bundle is written to content-addressed storage
first. The PostgreSQL transaction then locks the exact `dataset_snapshot` and
ordered `dataset_member` rows, proves that model input provenance is precisely
the membership digest followed by those feature bundle digests, registers the
model object, and inserts one immutable `model_snapshot` row. A composite
foreign key retains the dataset snapshot ID and feature-membership digest.
Failures may leave an unreachable CAS object but cannot expose a model row.

Readers require a complete `ModelSnapshotRef`, use a read-only catalog
transaction, verify exact CAS metadata and bytes, strictly decode the canonical
bundle, and compare all stored projection values. PostgreSQL never reconstructs
a model in place of its bundle.

Releases are separate append-only approval events. An alias resolves to its
highest release sequence. A new idempotency key may advance an alias to a newly
approved model or record a new approval of the same model; exact retries reuse
the same event, and inconsistent reuse fails closed. Model publication never
implicitly changes a release.

The v0.1 `ModelAnalysisRequest` pins the model-compatible dataset snapshot ID
and feature-membership digest, not the richer dataset snapshot digest. The
existing `resolve_model_dataset` composition seam must verify the rich
`DatasetSnapshotRef` before fitting. Since dataset snapshot ID is immutable and
primary-keyed, publication still closes over exactly one cataloged rich dataset
row. Adding the rich digest directly to the public model request would require
a separately versioned contract decision.

Runtime roles are append/read only for analysis, read only for dashboard, and
deny capture all model access. Dashboard projections remain derived and are not
part of authoritative model publication.

## Consequences

- A caller can no longer manufacture a model object reference or projection.
- Dataset member substitution is rejected before model visibility.
- Publication and release retries have explicit, independent identities.
- Job fencing, model scheduling, dashboard projection, association, and tracks
  remain separate concerns.
