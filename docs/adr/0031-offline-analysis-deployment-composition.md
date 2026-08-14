# ADR 0031: Offline analysis deployment composition

Status: accepted for infrastructure rehearsal; scientific production promotion pending

## Decision

Offline analysis is one service process with exactly two durable job lanes:
`recording_analysis` and `model_analysis`. `build_station_plugin` composes the
existing PostgreSQL job repository, immutable catalogs, filesystem CAS readers,
atomic fenced committers, and common service lifecycle. It accepts only two
complete mappings keyed by exact algorithm/config artifact references. It has no
capture, radio, provider retrieval, mutable alias, directory scan, entry-point
discovery, or additional queue capability.

The deployment validates all required migration receipts and `leo_analysis`
role privileges before touching the configured CAS. CAS readiness is a bounded
write/fsync/unlink probe. PostgreSQL connections are operation-scoped and all
scientific inputs are resolved from immutable refs carried by jobs and datasets.

No runnable plugin is checked in while scientific promotion is blocked. A
refusal-only plugin was rejected because merely claiming a durable production
job mutates and fails it. The checked systemd artifact is therefore a
non-installable template that names an absent operator-owned station module. It
cannot claim work from the repository alone.

## Consequences

A station release must install an importable operator-owned module that records
approved exact factories and the shared CAS root, then name that module in its
materialized systemd unit. Unknown refs fail the fenced lease; there is no
fallback. Unit and real-PostgreSQL composition tests use injected test-only
registries and do not constitute a deployable scientific plugin.

Ephemeris provider access remains outside this worker. The model lane reads only
the exact already-archived normalized object. Capture cannot be imported or
called from this composition.
