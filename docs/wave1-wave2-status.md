# Wave 1 and Wave 2 status

Date: 2026-08-13

## Integrated result

| Component | Implemented slice | Remaining production gap |
|---|---|---|
| Platform | Filesystem CAS, atomic SigMF pair codec, in-memory atomic catalog, fenced leases, initial SQL | Driver-backed PostgreSQL transactions, roles, restore, GC |
| Capture | Plan engine, paired-radio fake, SQLite spool, restart-safe publication | Real Pluto/Pi adapter, disk-pressure policy, hardware soak |
| Recording analysis | Deterministic quality and compact PSD FeatureSet | Calibrated detectors, drift/covariance, promotion evaluation |
| Model analysis | Frozen-dataset per-receiver quality aggregate with uncertainty | LNB/satellite physical model, association and tracking |
| Ephemeris | Mocked Space-Track/HF retrieval, validation, temporal selection | Live auth transport, scheduler persistence, publication, SGP4 |
| Dashboard | Read-only repository and framework-neutral JSON API | PostgreSQL read adapter, served UI and load qualification |
| Integration | Public-port capture → publish → read → FeatureSet vertical slice | Model/dashboard composition and cross-boundary fault matrix |

The full suite passes 193 tests when optional NumPy/HDF5 spike dependencies are
present. The dependency-free lane passes 186 tests and skips the seven optional
format-comparison checks.

## Accepted pre-release correction

`RecordingWriter.begin` now receives the spool-allocated `RecordingId`
explicitly. Local destination names are storage details and never scientific
identity.

## Contract findings for the next version

These are recorded rather than silently worked around in v0.1:

- Model fitting needs a first-class immutable dataset reader port.
- Hardware readers should return or verify the exact pinned snapshot digest.
- Dashboard DTOs need public activity identity, stronger state enums/validation,
  track radio identity, and caller-controlled bounded page size.
- Retrospective `BEST_EPHEMERIS` needs a frozen objective, lookahead, and
  tie-break before implementation.

None of these permits a component to bypass current capability boundaries.
