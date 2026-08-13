# 0009: Independent-recording detector suite

Status: proposed for integration-steward approval

## Decision

The first scientific detector suite operates on exactly one recording segment
and one explicit sample window at a time. It has no ephemeris, satellite,
dataset-partition, fitted-model, or network capability. Version `0.1.0` emits
three scores on identical paired-receiver window coordinates:

| Method | Score | Supporting observations | Cost per window |
|---|---|---|---|
| `coarse-energy` | Minimum across receivers of FFT peak power / median background-bin power | Peak bin, frequency, bin width, peak and noise power for each receiver | `O(N log N)` time, `O(N)` memory |
| `periodic-coherence` | Minimum across receivers of magnitude-normalized complex autocorrelation at one configured lag | Numerator, normalization, lag and per-receiver score | `O(N)` time, `O(1)` auxiliary memory |
| `paired-common-mode` | Maximum normalized cross-channel coherence over configured integer delays | Delay, relative phase, gain ratio, residual differential-power fraction and conjugate diagnostic | `O(N D)` time, `O(D)` score memory |

The conservative minimum aggregation means both receivers must carry the
single-channel evidence. The paired score uses the declared RX ordering, but
its magnitude is invariant to channel swap; delay changes sign. A conjugated
comparison is diagnostic only and never silently repairs representation errors.

Feature extraction and firing decisions are separate. `DetectorSuiteConfig`
is included in the analysis configuration digest and contains no threshold.
`ThresholdRule` identifies its calibration dataset and has its own digest.
Applying it produces a firing row for every input `MethodScore`, without
filtering non-firings. Existing covariance code can therefore compare methods
only on exact `(segment, receiver-pair, start, stop)` windows and reports
missingness rather than imputing a non-firing.

Zero-energy and, by default, clipped windows are refused with stable reason
codes. A segment shorter than one complete window produces no score and an
explicit reason. Reader truncation or malformed paired CI16 is a hard input
error. Thresholds remain uncalibrated until a frozen calibration dataset is
available; a score is evidence, not a satellite-identification claim.

## Legacy comparison

The read-only legacy numerical oracle contains normalized complex-lag
correlation in `leo_tracker.radio.iq_evidence` and FFT peak/background ideas in
several scan paths. This decision retains those mathematical definitions, not
their orchestration or storage code. Intentional differences are:

An out-of-tree comparison on 4,096 seeded complex samples at lags 1, 7, 64,
and 511 found a maximum absolute difference of `1.74e-17` between Redux's
direct accumulation and legacy's FFT-based normalized complex correlation.
This is floating-point summation order, not a definition change.

- Redux uses one declared lag rather than selecting the largest result from a
  search and thereby hiding the look-elsewhere effect.
- Redux uses deterministic rectangular, demeaned FFT windows already present
  in its compact-PSD implementation; window choice is explicit in diagnostics.
- Redux preserves per-channel evidence and adds a paired least-squares
  common-mode/residual diagnostic rather than equating two independent peaks
  with a shared RF source.
- Redux performs no in-extractor thresholding, identity assignment, TLE lookup,
  or cross-recording accumulation.

## Validation and promotion gaps

Integer-exact paired CI16 fixtures independently check FFT bins and DFT power,
lag correlation, delay, phase, gain, channel swap, conjugation, drift between
windows, deterministic noise/null behavior, clipping, zero and short inputs,
and score-window alignment. This validates implementation behavior only.

Before thresholds can be promoted, calibration must include independent null
passes, controlled and injected positives, V5 gap boundaries, receiver/LNB
swaps, temperature and gain epochs, interference, clipping, and a sealed radio
epoch. Frequency drift within a window, colored/nonstationary noise, analog
group delay, IQ imbalance, and multiplicity across candidate bins/lags require
explicit calibration or later versioned methods. None is silently estimated by
version `0.1.0`.

## Compatibility

No public contract changes are required. The suite publishes existing
`FeatureObservation`, `MethodScore`, and `FeatureSetBundle` v0.1 contracts.
Consumers that do not know these method IDs may ignore them. A future method
definition change requires a new method version, never reinterpretation of
`0.1.0` scores.
