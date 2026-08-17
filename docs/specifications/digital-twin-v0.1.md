# RF digital twin v0.1

Status: additive experimental specification. It does not alter a published
capture, detector, calibration, or dashboard contract.

## Purpose

The digital twin produces deterministic, bounded complex samples and exact
truth for exercising blind Doppler trackers and Starlink edge-pilot candidate
searches. It is a test and comparison instrument. It is not a radio simulator
for operational control, a calibration corpus, or evidence that a real
recording contains Starlink.

`leo-tracker` may be consulted offline as a numerical oracle. It is never
imported or invoked by this component. The v0.1 implementation has no radio,
database, service, CAS, filesystem-discovery, deployment, or network port.

## Component boundary

The public values are in `leo_flow.contracts.digital_twin`; deterministic
generation, orchestration, comparison, and the canonical codec are in
`leo_flow.analysis.digital_twin`.

The generator owns:

- seeded selection of CFO, linear drift, quadratic acceleration, and amplitude;
- exact per-frame path, burst-presence, and per-receiver truth;
- complex sample synthesis for two through four receiver chains and at most two
  radio identities;
- deterministic surrogate-pattern generation and declared impairments;
- bounded canonical bundle encoding and decoding.

It does not own detector logic. A blind Doppler analyzer and an edge-pilot
analyzer are injected through `DigitalTwinDopplerAnalyzerPortV0_1` and
`DigitalTwinPilotAnalyzerPortV0_1`. Each receives only an observation and the
precommitted pattern bank. The truth object is deliberately absent from the
analyzer input.

## Exact Qin target and surrogate controls

`make_qin_pattern_v0_1` seals an externally supplied exact Qin Appendix-A
edge-pilot waveform into `DigitalTwinPilotPatternV0_1`. The source artifact
must use the published `org.leo-flow.starlink-edge-pilot-template` v0.1 schema,
and the edge-specific indices must be exactly lower `528..535` or upper
`488..495`. The canonical I/Q value digest and energy are checked by the
contract. Scenario provenance must include both the sample-value digest and
the source-template dependency digest.

Surrogates are generated before synthesis from a separate unsigned 64-bit
seed. Every exact sample magnitude is retained while a precommitted QPSK phase
is selected by SplitMix64. Therefore each surrogate has the same per-sample
magnitude profile and total energy as the Qin input, but a distinct canonical
identity. A scenario explicitly emits one of:

- no pilot pattern (`null`);
- the exact Qin pattern (`qin-exact`);
- one indexed deterministic surrogate (`deterministic-surrogate`).

The emitted identity is preserved in truth. An analyzer receives the complete
pattern bank but not the emitted identity.

## Scenario and signal model

The scenario request fixes the admissible ranges and a 64-bit seed. Four draws
select CFO, drift rate, acceleration, and non-null amplitude. Noise and
receiver streams use independently derived seeds.

For time `t` in seconds and receiver `r`, the pilot frequency offset truth is

```text
f_r(t) = CFO + LNB_r + drift_rate * t + 0.5 * acceleration * t^2
```

The synthesized carrier phase is its integral:

```text
phi_r(t) = phase_r + 2*pi * (
  (CFO + LNB_r) * t
  + 0.5 * drift_rate * t^2
  + acceleration * t^3 / 6
)
```

One pattern spans one 750 Hz Starlink frame. `period_frames`, `on_frames`, and
`phase_frames` define deterministic burst duty and on/off timing. The truth
contains one midpoint, physical pilot-presence bit, and frequency offset for
every frame and receiver.

Receiver configuration declares:

- radio and receiver-chain identities;
- a constant LNB offset;
- linear gain and initial phase;
- sorted missing-frame indices.

Missing frames remain explicit in observation metadata and contain canonical
zero samples. They are not silently replaced with noise.

## Impairments

All impairments are additive and deterministic for the request identity:

- complex AWGN, with the configured standard deviation applied independently
  to I and Q;
- sinusoidal frame-scale gain variation;
- stationary tones, which are contractually required to have zero drift;
- narrowband interferers with optional linear drift;
- burst-gated complex broadband noise;
- receiver-specific missing frames;
- receiver-specific CFO contributions through the LNB offsets.

The generator and PRNG identifiers are `rf-digital-twin-v0.1` and
`splitmix64-box-muller-v0.1`. The request carries an immutable generator
artifact reference, and its digest plus the Qin source-template digest must be
closed into provenance dependencies. SplitMix64 supplies uniform values;
Gaussian samples use a cached Box-Muller pair. Replaying the same request with
the same Python numerical environment produces identical canonical bytes and
bundle digest.

## Candidate-only analyzer results

Both injected analyzer result contracts require `candidate_only=true`. Pilot
results and combined trial analyses require
`calibrated_detection_count=null`. They may expose candidate tracks and
descriptive statistics, including:

- candidate score;
- conditioned-control score;
- candidate-minus-control margin;
- drift rate and acceleration;
- spectral peak excess (never mislabeled as SNR);
- track duration.

No threshold or detection bit exists in these contracts.

## Twin-versus-real comparison

`DigitalTwinRealDataSummaryV0_1` is a narrow, immutable input DTO. It carries
bounded real-data statistic values and a digest of the external summary; it
does not expose recordings, ORM rows, or storage paths.

`compare_digital_twin_to_real_v0_1` joins distributions only on exact
`(method_id, statistic)` identity. For each shared identity it reports count,
mean, population standard deviation, minimum, q10, median, q90, maximum, mean
and median differences, and empirical two-sample KS distance. Twin-only and
real-only identities remain explicit.

The resulting `DigitalTwinComparisonViewV0_1` is dashboard-ready but always
carries `candidate-only-comparison-not-calibration-or-detection`. Distribution
distance is descriptive. It does not select a threshold, estimate a false
alarm rate, calibrate a detector, or claim a detection.

## Resource and serialization bounds

The contracts fail closed at:

| Resource | Bound |
|---|---:|
| Pattern samples | 16,384 |
| Surrogates | 32 |
| Frames | 512 |
| Receiver chains | 2–4 |
| Radio identities | 1–2 |
| Total generated complex samples | 1,048,576 |
| Tone/interference sources | 16 |
| Candidates per analyzer output | 32 |
| Statistics per analyzer output/summary | 128 |
| Values per real distribution | 4,096 |
| Distribution comparisons | 256 |
| Canonical bundle JSON | 64 MiB |

`encode_digital_twin_bundle_v0_1` emits the repository canonical JSON domain.
`decode_digital_twin_bundle_v0_1` rejects oversized input, malformed schema,
non-finite values through contract reconstruction, and any semantically valid
but non-canonical byte representation. Exact decode/re-encode equality is the
replay gate.

## Validation expectations

Component-owned tests cover:

- same-seed byte/digest reproducibility and different-seed sensitivity;
- deterministic, distinct, energy-matched surrogates;
- burst duty and exact linear/quadratic/LNB path truth;
- null, AWGN, gain, tone, narrowband, broadband, missing-frame, two-chain, and
  two-radio behavior;
- truth exclusion from injected analyzer inputs;
- candidate/control comparison against a real-summary DTO;
- canonical codec rejection behavior;
- contract resource and provenance failures;
- absence of reference-runtime, live-system, storage, and service imports.

## Known limits

v0.1 uses a direct time-domain model and does not model antenna patterns,
atmospheric propagation, quantizer clipping, analog filter group delay,
oscillator phase noise, or a complete Starlink OFDM payload. Exact Qin samples
are injected from their published artifact rather than recreated from a private
implementation. These omissions must be closed with controlled truth and real
recording comparisons before any later calibration use is proposed.
