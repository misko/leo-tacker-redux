# ADR 0019: Offline orbit and RF association boundary

Status: accepted

## Decision

Introduce a pure analysis boundary between archived ephemeris evidence and RF
association. An association request closes over:

- the full recording–ephemeris link projection, whose artifact ID and digest are
  recomputed before use, plus its exact raw/normalized snapshot reference;
- exact feature-set/feature, recording, hardware, receiver-chain, and station
  geometry identities;
- propagator implementation, gravity constants, time-scale, Earth-orientation,
  and propagation-error policy artifacts;
- transmitter carrier hypotheses and receiver/LNB bias, drift, and uncertainty;
- explicit elevation, residual, ambiguity, and tie policy.

The propagator is an injected offline port. This slice provides a deterministic
exact-key simulator for tests and synthetic experiments, not an SGP4
implementation. No dependency is added: selecting an SGP4 package before a
validated adapter/corpus exists would falsely imply scientific readiness. A
future adapter must read the exact normalized TLE object offline and identify
its package/version and all numerical choices through the existing propagation
specification.

The association core predicts first-order received carrier frequency and drift
from range rate/acceleration, adds pinned receiver/LNB calibration, and scores
frequency and drift residuals by their combined variances. Candidates below the
elevation gate, with propagation errors, or outside the residual gate are
explicitly rejected. Candidates are ordered by score then NORAD ID. Near-equal
best candidates return `ambiguous`; tie-breaking never silently creates a
match.

## Consequences

An association decision is reproducible evidence, not ground truth. Accuracy
claims still require independently reviewed or digitally injected labels. The
boundary has no network, credentials, raw IQ, capture, publication, dashboard,
or mutable-latest lookup. Kalman filtering and multi-recording tracking remain
out of scope until real SGP4 adapter validation and association identity are
mature.
