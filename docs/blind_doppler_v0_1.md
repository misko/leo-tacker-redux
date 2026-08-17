# Blind Doppler candidate tracking v0.1

This additive component finds narrow, time-continuous spectral tracks without an
ephemeris or a detection threshold. Its output is candidate-only evidence. It
must not be interpreted as a Starlink classification or promoted to a capture
gate without a separately versioned, calibrated detector.

## Public boundary

The tracker consumes `SpectrogramSliceV0_1` through
`BlindDopplerSpectrogramPortV0_1`. The input contains only an immutable source
digest, segment and receiver identities, a frequency axis, UTC row midpoints,
and finite power rows. It deliberately has no dependency on waterfall-v0.2,
its storage layout, codecs, ORM models, or implementation classes.

An integration-owned adapter may translate any public spectrogram product into
this input contract. The application layer then constructs a request whose
`config_digest` equals `blind_doppler_config_digest(config)`, invokes
`BasicBlindDopplerAnalyzer.analyze_blind_doppler`, and persists the strict
canonical bundle through its own existing output port. No adapter, persistence,
dashboard, deployment, or schema migration is part of this component patch.

## Bounded analysis

1. Estimate each row's median noise floor and suppress rows with broadband
   excess. This makes additive AGC steps a control rather than a track.
2. Extract bounded local maxima above the row floor. Interior peaks use a
   three-point parabolic sub-bin interpolation; edge peaks are retained with an
   explicit truncation flag.
3. Build time-forward continuity links with configured gap, frequency-step,
   and drift-rate bounds, then form connected components.
4. Seed velocity-aware paths inside each component. Seed count, peaks per row,
   input cells, output candidates, and points per candidate are hard bounded.
5. Robustly fit constant, linear, and—when enough points exist—quadratic
   frequency models with Huber reweighting. BIC selects the descriptive model;
   all supported fits remain in the evidence.
6. Rank a bounded top-K using SNR, duration, row coverage, and explicit edge
   penalties. The bundle retains power, duration, missing-row, fit residual,
   and stationary-control evidence.

The quadratic convention is
`f(dt) = f0 + drift_rate*dt + 0.5*drift_acceleration*dt^2`, referenced to the
middle track point's UTC time.

## Integration seam

```text
public spectrogram producer
        |
        | integration-owned adapter
        v
BlindDopplerSpectrogramPortV0_1 -> SpectrogramSliceV0_1
        |
        v
BasicBlindDopplerAnalyzer -> BlindDopplerBundleV0_1
        |
        | integration-owned persistence/projection adapter
        v
existing immutable analysis artifact flow
```

The strict codec rejects unknown or duplicate fields, noncanonical JSON,
non-finite values through the contracts, unsupported schema versions, and
oversized payloads.
