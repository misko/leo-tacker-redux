# Starlink published edge-pilot constellation v0.1

## Scope

This additive diagnostic demodulates the 300-by-8 published Qin edge-pilot
coefficients from one receiver stream. It is a 4QAM/QPSK constellation of known
synchronization structure. It does not decode a Starlink header or user payload,
and it is candidate evidence rather than a calibrated detection.

The input is bound to an immutable Starlink detector-suite v0.2 stream and the
suite's `full-frame-acquire` winner: recording, segment, receiver, edge, epoch,
coarse CFO, search identity, algorithm/config, and exact-template identities are
all closed into the output. The output cannot change detector selection.

## Numerical method

Redux implements the reference method natively. `leo-tracker` is a numerical
oracle only and is never imported at runtime.

1. Remove the acquire winner's coarse plus reported residual CFO.
2. For every complete 750 Hz frame and symbols 2 through 301, solve the eight
   edge-subcarrier coefficients by complex least squares over that OFDM symbol.
3. Estimate a bounded residual CFO from known-code-removed within-frame phase
   slopes and correct it about the mean pilot-symbol time.
4. Align frames by their known-pilot match phase. Weight the stack by clipped
   normalized match quality.
5. Equalize each symbol parity with an eight-subcarrier channel estimate learned
   only from the opposite parity. This cross-fit prevents a coefficient from
   fitting its own channel estimate.
6. Compare the 2,400 stacked coefficients with the rotated QPSK constellation
   `exp(j*pi/2*(state+0.5))`. Persist hard accuracy, RMS EVM, soft posterior
   summaries, channel facts, and bounded plot points.

The soft posterior uses the observed mean squared error with a `1e-6` floor. Its
confidence and entropy are visualization diagnostics, not calibrated
probabilities of Starlink presence.

## Resource and failure bounds

The analyzer rejects non-finite/empty input, rates below the eight-subcarrier
occupied bandwidth, inputs beyond its declared sample ceiling, excessive frame
support, a mismatched suite identity, or windows without a complete frame. The
artifact always contains exactly 2,400 canonically ordered stacked plot points,
under the public `MAX_CONSTELLATION_POINTS` ceiling.

## Oracle provenance

The equations and expected fixture values were checked against the pinned
`leo-tracker/src/leo_tracker/radio/beacon/decode.py` implementation in the sibling
reference checkout at Git commit
`0bb80d14759fd8496b74e7d3219a690be18565a6` on 2026-08-17. The pinned decoder
source SHA-256 is
`2ed3a4ed87c14a6bc028539bc83a671e100d9fe6b35732f2db14adab05273f84`.
Component tests contain fixed oracle numbers; they do not import the reference
repository and golden files are not regenerated when a test fails.
