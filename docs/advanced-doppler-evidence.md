# Advanced Doppler evidence and validation

Status: source-integrated release candidate. The reviewed kernels are wired into
the combined waterfall analysis worker, additive PostgreSQL persistence/query
ports, and the recording-level dashboard visualization. They are not yet sealed,
migrated, or enabled in the live deployment. All outputs remain candidate-only;
orbit association is optional post-blind evidence and is never a detection claim.

## Scientific boundary

The system must first establish a blind, receiver-independent moving structure.
Only then may it compare that frozen track with TLE predictions. A close TLE
curve is an association result, never signal-detection evidence.

The prototype in `leo_flow.analysis.doppler_evidence` is a dependency-free
numerical oracle for that boundary. Production code may optimize its kernels,
but must preserve its fit/held-out splits, controls, nuisance parameters, and
failure semantics.

## Evidence flow

| Stage | Positive evidence | Required control | Output allowed forward |
|---|---|---|---|
| Direct de-Doppler | Held-out power on a slope-bank path | Stationary path, opposite slope, time-shuffled rows | Blind linear path plus scores |
| Viterbi and peeling | Continuous ridge; additional ridges after masking | Stationary maximum and minimum movement | Independent ridge population |
| Comb | Odd held-out teeth on the fitted path | Even teeth used only for fitting; wrong spacing | Cross-validated comb support |
| Broadband | Lower and upper edges share motion; texture translates | Width stability and centroid-redistribution confound | Edge/texture support |
| Dual receiver | Common slope/shape after one constant offset per receiver | Slope mismatch, offset-removed RMS, path correlation | Receiver-independent blind track |
| TLE association | Lowest held-out residual after constant offset | Stationary, opposite-motion, runner-up/wrong TLE | Qualified association or ambiguity |

No single row in this table is a Starlink claim. A production policy must
require the applicable rows together and preserve every intermediate score.

## Algorithms

### Direct slope bank

For each slope `s` and intercept `b`, sample nearest bins
`round(b + s * row)`. Rows are split deterministically by index modulo three:
training rows fit each slope's intercept, validation rows select the slope, and
disjoint test rows produce the reported score. After selection, training and
validation rows jointly refit only the intercept. This prevents the selected
slope from using the same noise excursions reported as final evidence. Compare
the test score with independently refitted stationary and opposite-slope paths
and deterministic cyclic permutations of test-row identity. A production
empirical false-alarm probability is
`(1 + null >= observed) / (N + 1)` using the maximum intercept score from each
permutation, not the score from one preselected intercept.

Public configuration is expressed in physical `Hz/s`. The recording adapter
derives bins per row from each tile's exact median row cadence and frequency-bin
width. A fixed bins-per-row grid is invalid across sample rates, FFT widths, and
row aggregation. The qualified default grid must include fine support around the
report-observed few-kHz/s population as well as bounded broader tails.

### Viterbi and multi-track peeling

Dynamic programming maximizes ridge power subject to a maximum per-row bin
step and an optional motion penalty. After accepting a path, mask a configured
neighborhood and solve again. Promotion requires stable recovery of separated
tracks and explicit handling of crossings; the prototype does not claim a
probabilistic uncertainty model.

### Comb and broadband support

Comb paths are fitted with even-numbered teeth and validated with odd-numbered
teeth. A wrong-spacing control must fail. Broadband support is stronger when
independently measured lower and upper edges have the same slope, bandwidth is
stable, and high-frequency internal texture translates by the same step. A
moving centroid alone is insufficient because changing subchannel power can
move a centroid without translating the emission.

### Dual-radio/LNB consensus

Two receiver paths are compared after fitting a constant frequency offset for
each receiver. The prototype fixes receiver zero as the gauge origin, so its
offset tuple is `(0, relative_offset)`. This absorbs arbitrary LNB/local-oscillator offsets without erasing
slope or curvature disagreement. Production qualification must additionally
record receiver geometry: two channels on one radio share confounds and are
not equivalent to independent radios/LNBs.

For longer windows, extend the path model with curvature, but keep receiver
offsets independent and motion coefficients common. Do not fit a free slope per
receiver and then call the paths common-motion.

### Post-blind TLE association

TLE predictions cannot enter the detector or tune its thresholds. Freeze the
blind track first. For every TLE, fit only an allowed constant receiver offset
on even rows and rank odd-row RMS. Qualification requires a predeclared margin
over the runner-up and lower error than stationary and opposite-motion
controls. Time-shifted ephemerides and wrong satellites belong in production
validation even though the small oracle exposes the stationary/opposite pair.

## Frozen oracle strategy

The fixture
`tests/advanced_analysis/fixtures/doppler_evidence_oracle_v0_1.json` records the
reference repository commit, exact source files audited, deterministic inputs,
and numerical expectations. Tests import only `leo_flow`; they never import
`leo-tracker`. Update this fixture only after an explicit oracle review that
explains a scientific change. A failing implementation test is not permission
to regenerate it.

Reference ideas were audited at `leo-tracker` commit
`0bb80d14759fd8496b74e7d3219a690be18565a6`, principally:

- `radio/tracking/dedoppler.py`: alternating-row fit/held-out slope bank and
  maximum-intercept permutation null;
- `radio/tracking/viterbi.py` and `radio/blind_comb.py`: continuity-constrained
  paths, ridge peeling, and even/odd tooth cross-validation;
- `radio/tracking/broadband.py`: independent edges and texture translation;
- `radio/tracking/association.py`: receiver consensus with arbitrary constant
  LNB offset;
- `radio/tracking/tle_match.py`: association only after blind qualification.

## Real-recording source qualification

The source-integrated path was exercised offline on the synchronized 20-second
CH4-lower c04 pair (`rec_01M08G2JW3DM7AP6PE46EA4BBM` and
`rec_01M08G2JW3FDHYY0XXS5PMMYGG`). Each of four segment/receiver tiles processed
1,525 non-overlapping FFT frames and 49,971,200 of 50,000,000 samples
(99.9424% coverage) into a 200-by-512 display. The basic tracker honestly emitted
zero candidates, while the independently valid advanced path retained three
peeled tracks and train/validation/test slope-bank controls at approximately
-3.5 to -3.75 kHz/s. This `advanced-path-only` state is preserved rather than
coerced into or hidden behind a basic candidate.

Both recordings completed the combined waterfall/Doppler worker in 7.39--8.83
seconds at about 120 MiB RSS. All six bounded V9 requests (two recordings by
three layers) returned HTTP 200 with per-tile provenance, physical drift, controls,
and no calibrated-detection field. Checksummed evidence is retained at
`~/.local/state/leo-flow/evidence/doppler-c04-real-qualification-20260817-v1`.
The qualification used a disposable PostgreSQL 16 database and output CAS; both
were removed after verification, and no radio, live database, dashboard, capture
journal, or production CAS mutation occurred.

The reference is a numerical oracle and is never a runtime dependency.

## Benchmark and acceptance matrix

| Case | Corpus requirement | Acceptance gate |
|---|---|---|
| Positive/negative weak chirp | Seeded noise, signal below per-row threshold, both signs | Slope error <= 0.25 bin/row; held-out score exceeds every declared control |
| Stationary interferer | Strong stationary line with weaker moving ridge | Moving ridge recovered; stationary improvement positive; stationary-only case never qualifies |
| Multiple/crossing ridges | At least 2 paths, varied separation and SNR | Two-track recall >= 0.95 away from crossing; median path error <= 1 bin |
| Comb | Missing teeth, amplitude imbalance, wrong spacings | Held-out odd-teeth margin positive; all predeclared wrong spacings fail |
| Broadband translation | Stable edges with internal power redistribution | Edge slope difference <= 0.25 bin/row; width MAD fraction <= 0.20; texture correlation >= 0.80 |
| Centroid confound | Fixed edge plus changing subchannel power | No translation qualification |
| Dual independent receivers | Different constant LNB offsets and independent noise | Slope difference <= 0.25 bin/row; offset-removed RMS <= 1 bin; correlation >= 0.90 |
| Receiver disagreement | Opposite/different slopes or time shift | Consensus fails even if either receiver qualifies locally |
| Post-blind TLE | Correct, wrong, time-shifted, stationary, opposite curves | Correct held-out RMS wins by declared margin; near ties remain ambiguous |
| Noise-only trials | At least 999 deterministic trials per operating point | Family-wise false-positive rate <= predeclared alpha with max-statistic null |
| Runtime envelope | 256 rows x 4096 bins, fixed slope/path limits | Benchmark p95 and memory budget frozen before production implementation |

Benchmark results must be stratified by SNR, drift, curvature, duration,
frequency edge proximity, comb occupancy, bandwidth, and receiver independence.
Aggregate accuracy alone is not a promotion gate.

## Promotion gates

1. Freeze a versioned public evidence contract and units; this prototype's
   dataclasses are internal and are not that contract.
2. Reproduce the oracle fixture and all synthetic acceptance cells without a
   runtime reference-repository import.
3. Add time-shifted ephemeris and maximum-statistic permutation tests with at
   least 999 trials in the benchmark lane.
4. Calibrate thresholds on one corpus and report untouched held-out performance
   on another, including noise-only and known stationary emitters.
5. Demonstrate dual *independent* radios/LNBs; label shared-clock/shared-radio
   evidence separately.
6. Prove deterministic resource bounds and define cancellation/checkpoint
   behavior before attaching the implementation to analysis workers.
7. Have the integration steward approve contracts, dependencies, persistence,
   deployment, and cross-component tests before pipeline wiring.
8. Keep TLE association downstream of an immutable blind-evidence digest and
   retain ambiguous/no-association outcomes as first-class results.
