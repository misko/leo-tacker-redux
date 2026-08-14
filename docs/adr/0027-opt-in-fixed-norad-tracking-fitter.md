# ADR 0027: Opt-in fixed-NORAD tracking fitter

Status: experimental

## Decision

Expose the experimental fixed-NORAD residual tracker as an injected
second-stage `ModelFitter` that a deployment may register under one exact
algorithm/configuration key. It is not registered by default. The model
configuration content-addresses the feature selector, extractor implementation,
carrier hypotheses, station geometry, propagation choices, association policy,
and tracking policy. The injected extractor must expose the same immutable
artifact reference carried by the configuration.

The fitter opens every `FeatureSetRef`, `EphemerisSnapshotRef`, and
`HardwareMetadataSnapshotRef` named by the `ModelAnalysisRequest`. It verifies
returned identities, and verifies the materialized hardware content digest.
The model architecture does not permit fitters to read ephemeris bytes; the
injected orbit adapter remains responsible for verifying normalized bytes
against the exact ephemeris ref before propagation. The extractor sees only
those materialized exact values; it receives no catalog, path, clock, network,
or mutable resolver capability. Its output is checked back against the
selected FeatureObservation values and covariance, the request's ephemeris and
hardware refs, the recording identity digest, effective receiver chain, and
station identity. The fitter rejects omitted or duplicate selected features.

Association remains an evidence-generating decision. Only matches to the one
configured NORAD ID enter the residual filter; ambiguity, no-match, propagation
errors, and identity mismatches remain explicit rejections. Model parameters
contain residual frequency/drift estimates, not orbit state. Snapshots include
the algorithm, config, extractor, station, propagation, association, tracking,
dataset, FeatureSet, hardware, and ephemeris digests in identity/provenance and
carry explicit `experimental:not-satellite-truth` warnings.

## Fail-closed FeatureSet v0.1 boundary

FeatureSet v0.1 can represent measured `frequency_hz`, `drift_hz_s`, receiver
chain, midpoint UTC, and a covariance. That is enough to verify an extracted RF
measurement, but not enough to construct a truthful association input. It does
not contain:

- the authoritative recording interval and complete recording-to-ephemeris link
  evidence, including selection scope, policy, cutoff, and link identity;
- effective receiver RF calibration: frequency bias, drift, their variances,
  and the exact hardware snapshot reference;
- a typed declaration that covariance basis and units describe the absolute RF
  frequency and drift consumed by association.

`UnsupportedV01FeatureTrackingExtractor` therefore always fails. A future
recording analyzer output (or a new typed immutable artifact referenced by that
output) must publish those fields. Its content-identified extractor may then
convert them without consulting another service. Until that output exists,
production composition must not register this fitter.

## Consequences

The integration exercises the real offline registry, exact readers, RF
association, residual filter, ModelSnapshot serialization boundary, and durable
model publication without pretending current recordings contain missing
evidence. The frozen provenance contract records the top-level config digest;
that content digest closes over the extractor, station, propagation,
association, tracking, and carrier choices. Configuration changes alter that
digest. Input changes alter model identity/provenance. There is no latest
lookup, filename convention, network fetch, automatic NORAD switching,
production release, or ground-truth claim.
