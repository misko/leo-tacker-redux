# ADR 0025: Experimental offline residual tracking

Status: experimental

## Decision

Add a dependency-free offline prototype that filters multiple, time-ordered RF
measurements only after each measurement has a validated association decision.
The input closes over the exact `RfAssociationRequest`; its canonical digest
must equal the decision's request digest. Receiver chain, hardware snapshot,
station geometry, feature, recording, ephemeris, calibration, and propagation
identities therefore cannot be substituted independently.

One run fixes one NORAD ID. Its state basis is explicitly
`[frequency_residual_hz, frequency_drift_residual_hz_s]` relative to the
association candidate's RF prediction. It is not position, velocity, TLE, or
orbital state. The transition is constant residual drift. Process covariance is
the continuous white drift-noise discretization
`q * [[dt^3/3, dt^2/2], [dt^2/2, dt]]`. The measurement observes both state
components directly. Measurement covariance combines the feature measurement
variance with caller-supplied prediction/calibration variance.

Every state, covariance, prediction, innovation, innovation covariance, and
normalized innovation squared value remains in the report. Updates beyond a
pinned NIS gate are rejected. The optional backward pass is the standard
fixed-interval Rauch–Tung–Striebel recursion over the same two-state model.

Ambiguous, no-match, malformed, propagation-error, or different-NORAD
decisions terminate the current segment and are reported as rejected. Input
time must be strictly increasing. Excessive gaps and changes to receiver,
hardware snapshot, or station geometry start a new segment only when the
specification explicitly enables that behavior; otherwise the observation is
rejected. Identity is never switched within a segment.

## Consequences and limits

This is an experiment, not a production Kalman filter and not a satellite
tracker. Association remains evidence rather than ground truth. The prototype
trusts the candidate values in a structurally valid association decision; the
current association boundary has no signed or content-addressed decision
artifact. It does not estimate orbit, carrier assignment, LNB parameters, or
cross-receiver transformations. It has no persistence, job, dashboard,
network, or embedded runtime integration.

Validation uses a separately implemented, digest-seeded digital observation
source and pinned fixture. Those labels are software truth only. Passing them
does not demonstrate accuracy on real satellite recordings. Promotion requires
reviewed real labels, calibrated uncertainty, residual diagnostics, numerical
stress testing, and an immutable association-result boundary.
