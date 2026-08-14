# ADR 0021: Pinned offline SGP4 propagation adapter

## Status

Accepted.

## Decision

Model analysis may use `Sgp4OrbitPropagator` behind the `OrbitPropagator`
port. The adapter is an optional installation (`orbit`) and pins `sgp4==2.25`,
the package's Vallado implementation in improved operation mode, and the WGS72
constant set. Importing the dependency-free association core does not require
the optional package; constructing this adapter fails clearly if the exact
version is unavailable.

The adapter reads only `EphemerisReader.normalized_bytes()` for the exact
`EphemerisSnapshotRef`. It verifies both the returned identity and normalized
SHA-256 digest before selecting the exact NORAD ID. It never retrieves TLEs or
Earth-orientation data from the network.

The first profile is intentionally modest and fully identified:

- integer Unix UTC nanoseconds are supplied to SGP4 with UT1−UTC fixed to zero;
- TEME is rotated to PEF with the Vallado GMST 1982 expression;
- polar motion is zero, so PEF and ITRF are treated as coincident;
- the station is fixed in the rotating frame and its geocentric position is
  used as local vertical;
- range acceleration is a centered difference of range rate over exactly one
  second; and
- any non-zero SGP4 status returns `sgp4:<code>` with zero observables, while
  archive, identity, configuration, and missing-NORAD faults fail closed.

Each choice is content-addressed in `PropagationSpecification`. The adapter
rejects any other specification rather than silently approximating it.

## Consequences

This profile is deterministic and adequate for offline experiments, but it is
not a precision orbit-determination system. It does not apply measured DUT1,
leap-second tables, polar motion, nutation, precession, atmospheric refraction,
or a geodetic local vertical. A higher-accuracy implementation must have new
artifact identities and verification fixtures; it must not mutate this profile.

The compact golden fixture preserves published Vallado verification TLEs and
TEME vectors verbatim. The legacy error-case checksums are also preserved. A
test-only derivative corrects only those two checksum digits so the strict
normalized-archive parser can exercise the published SGP4 error-code path
without weakening archive validation or rewriting the authoritative fixture.

## Validation

- Published Vallado SGP4 TEME vectors at epoch and +360 minutes.
- Published case 33333 error code 4 at +25 minutes, through the explicitly
  identified checksum-only derivative.
- Frozen range rate, centered range acceleration, and geocentric elevation.
- Exact snapshot identity, digest, NORAD membership, and specification gates.
- Optional-dependency absence and wrong-version failure at adapter construction.
