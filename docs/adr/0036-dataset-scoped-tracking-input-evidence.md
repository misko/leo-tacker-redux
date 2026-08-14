# ADR 0036: Dataset-scoped tracking input evidence

Status: accepted for contract, codec, and deterministic builder; persistence and
promotion pending

## Decision

FeatureSet v0.1 remains the immutable output of independent recording analysis.
It is not revised to contain ephemeris selection, receiver calibration, or
cross-recording prediction policy. Those facts can change independently and
belong to wider analysis.

Before a tracking model job is admitted, wider analysis freezes one canonical
`TrackingInputSnapshot` for the exact durable dataset. Each ordered entry binds
the exact FeatureSet and feature identity, authoritative recording identity and
half-open interval, complete hardware and ephemeris links, an ABSOLUTE_RF
frequency/drift measurement with full 2x2 covariance, required immutable
receiver calibration with validity and source provenance, and a separate
prediction covariance and policy. There is no zero/default calibration.

The snapshot and its reference exclude replaceable object locators from
scientific identity while binding digest, byte count, media type, and format.
The strict bounded codec rejects unknown or missing fields, duplicate JSON
keys, noncanonical bytes/order, non-finite values, invalid covariance, and open
provenance. Contract-local mirrors of `DatasetSnapshotRef` and `FeatureSetRef`
avoid reversing package dependencies; later assembly must convert them through
exact equality.

## Consequences

Independent analysis remains replayable without provider, catalog, or hardware
lookups. Wider tracking receives a single immutable scientific join and never
needs raw IQ, filesystem paths, clocks, provider clients, or mutable aliases.
Relinking ephemeris or publishing a new calibration creates a new tracking
input snapshot rather than rewriting a FeatureSet.

The deterministic builder validates exact ordered dataset membership, source
reference equality, recording intervals, link and selection policy closure,
measurement covariance, calibration, prediction policy, and provenance without
opening IQ or consulting a path, catalog, provider, clock, or network. It treats
the authoritative FeatureSet reader boundary as responsible for byte
verification rather than importing independent-recording implementation code.

The current experimental tracking fitter remains fail-closed. Production use
still requires authoritative catalog/CAS publication, a versioned model-request
input reference, correlated covariance math, and held-out scientific promotion
evidence.
