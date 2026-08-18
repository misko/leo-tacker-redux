# Legacy `pilot_symbolwise_v3` replay audit and native Redux v0.1

Date: 2026-08-18 UTC  
Status: additive component implemented and externally reproduced; candidate evidence only  
Redux base: `271619768f242d99d1fbf8440cdca7928591d89a`  
Legacy numerical oracle: `leo-tracker@0bb80d14759fd8496b74e7d3219a690be18565a6`

## Result

Redux now has a native, bounded replay of the historical
`pilot_symbolwise_v3` statistic. It does not import or execute `leo-tracker`.
For each receiver it evaluates one exact 10 ms window every 100 ms and emits
the complete Qin target evidence plus four precommitted random-QPSK surrogate
searches. Every pattern independently repeats the same timing, CFO, candidate,
symbolwise, and conditioned search before it may be selected.

The strongest retained J1 window at 41.6 s reproduces both historical receiver
winners. Epochs and CFOs agree within numerical tolerance, symbolwise margins
agree to machine precision, and conditioned margins differ by at most
`2.7e-7`. The retained RETRO 68.7 s positive also reproduces both historical
winners.

This component does **not** convert a candidate into a calibrated detection.
Four finite surrogates are controls, not an empirical null distribution, and
the historical roll-17 score is conditioned on the Qin-selected hypothesis.

## Exact historical cadence and statistic

The oracle implementation was audited in:

- `src/leo_tracker/radio/beacon/acquisition.py`:
  `acquire_exact_receiver(..., method="pilot_symbolwise_v3")`
- `src/leo_tracker/radio/beacon/pilots.py`:
  `acquire_pilot_epoch`, `track_edge_pilots`,
  `conditioned_pilot_frequency_search`, and `conditioned_pilot_score`
- `src/leo_tracker/radio/beacon/analysis.py`: the 10 ms / 100 ms replay loop
- `src/leo_tracker/radio/beacon/templates.py` and `structure.py`: waveform and
  frame constants

At 2.5 MS/s the exact historical per-window sequence is:

1. Read 25,000 complex samples (10 ms).
2. Search all 3,333 integer frame epochs using 24 linearly spaced pilot-symbol
   anchors and nine CFO hypotheses at receiver center + `[-320, -240, ...,
   +320]` kHz.
3. Retain four timing candidates separated circularly by at least 20 samples.
4. At each candidate, search `candidate CFO + {-100, 0, +100}` kHz, clipped to
   receiver center +/-350 kHz. Select by mean normalized per-symbol power and
   refine CFO from the symbol-correlation phase slope.
5. Search full-frame normalized magnitude over +/-2 kHz at 100 Hz about the
   symbolwise estimate.
6. Evaluate the 17-symbol-rolled waveform only at the target winner. Select
   the timing candidate by
   `max(full-frame margin, 0) * max(symbolwise margin, 0)`, then full-frame
   margin, then symbolwise margin.
7. Repeat at starts `0.0, 0.1, ..., 59.9` s for a 60 s dwell.

The historical decision gates were:

| Gate | Historical value |
|---|---:|
| Dual-receiver match margin | 0.02 |
| Dual-receiver symbolwise margin | 0.02 |
| Dual-receiver epoch delta | <=20 samples |
| Single-receiver match margin | 0.025 |
| Single-receiver symbolwise margin | 0.03 |
| Qualified match margin | 0.05 |
| Qualified symbolwise margin | 0.05 |
| Qualified coherence | 0.05 |
| Qualified epoch delta | <=8 samples |

Those gates are recorded here for parity only. The new component deliberately
does not apply them because their whole-search false-alarm behavior is not
calibrated in Redux.

## Redux interfaces

The additive analysis API is:

```python
class StarlinkSymbolwiseWindowReaderV0_1(Protocol):
    def read_window(self, start_sample: int, sample_count: int) -> Sequence[complex]: ...

StarlinkSymbolwiseReplayAnalyzerV0_1.resource_plan(
    *, sample_rate_hz, segment_sample_count, frequency_center
) -> StarlinkSymbolwiseReplayResourcePlanV0_1

StarlinkSymbolwiseReplayAnalyzerV0_1.analyze_receiver(
    reader,
    *, recording_id,
    recording_identity_digest,
    segment_id,
    receiver_chain_id,
    edge,
    sample_rate_hz,
    segment_sample_count,
    frequency_center,
) -> StarlinkSymbolwiseReplayBundleV0_1
```

`StarlinkReceiverFrequencyCenterV0_1` is a required immutable input containing:

- an explicit absolute CFO center relative to the recording IF center;
- the calibration source artifact and digest;
- hardware-epoch digest;
- physical receiver-signal-path digest; and
- a required assertion that the value was fixed before replay.

No code derives a center from `lnb-a/b/c/d`, a display label, or a legacy swap
label. A component test permutes `ReceiverChainId` while keeping the IQ and
frequency-center object together and proves that all numerical window evidence
is unchanged.

## Why this is additive to v0.3

| Property | Legacy `pilot_symbolwise_v3` | Published Redux v0.3 | Additive replay v0.1 |
|---|---:|---:|---:|
| Anchor symbols | 24 linear | 12 (`2..288` step 26) | 24 linear |
| Initial CFO grid | center +/-320 kHz, 80 kHz | absolute -400..+400 kHz, 80 kHz | center +/-320 kHz, 80 kHz |
| Candidate count | 4 timing epochs | 8 epoch/CFO basins | 4 timing epochs |
| Fine CFO | three coarse points, phase slope, then +/-2 kHz / 100 Hz | +/-80 kHz / 500 Hz plus quadratic peak | legacy sequence |
| Pilot use | all symbols in symbolwise score | even acquire / odd held-out verify | all symbols in symbolwise score |
| Historical roll-17 | conditioned at target winner | held-out exact/control | exposed as conditioned legacy control |
| Random-pattern controls | absent | separate paired-surrogate suite | 1-4 patterns, full identical replay |
| Decision | fixed historical gates | calibrated contract required | candidate evidence only |

Published v0.3 requires absolute `-400..+400` kHz coverage. Its bounds must not
be silently shifted. A future production integration has two honest choices:

1. Publish a new v0.4 contract whose search coordinates explicitly comprise a
   receiver center plus a residual domain; or
2. Retain immutable v0.3 by frequency-translating samples in a separately
   versioned preprocessing component whose calibration identity is explicit in
   provenance, then give v0.3 its unchanged absolute residual-domain input.

This work does neither. It supplies a separate legacy-parity evidence contract.

## Pattern-symmetric controls

The pattern order is canonical: Qin first, followed by SplitMix64-derived,
precommitted random QPSK patterns with codebook indexes 0 through 3. For every
pattern the analyzer repeats:

- 29,997 timing cells per window at 2.5 MS/s;
- four retained candidate refinements;
- 188 explicitly accounted refinement cells;
- its own timing and CFO winner selection; and
- its own 17-symbol cyclic-roll companion at its selected hypothesis.

Thus the random patterns measure the selection benefit of the complete search,
not merely a score at the Qin winner. They remain only four deterministic
controls. The bundle reason codes explicitly state
`finite-pattern-controls-not-empirical-null` and
`whole-search-calibration-required`.

## Coverage and resource accounting

For a 60.000 s, 2.5 MS/s dwell with Qin plus four surrogates:

| Quantity | Exact value |
|---|---:|
| Window / cadence | 10 ms / 100 ms |
| Window starts | 600 (`0` through `149,750,000`) |
| Samples per window | 25,000 |
| Analyzed union | 15,000,000 samples = 6.000 s |
| Union coverage | 10.000% |
| Timing cells | 89,991,000 |
| Refinement cells | 564,000 |
| Conservative per-window working bound | 6,400,000 bytes |

The resource plan is computed and checked before any IQ read. Defaults cap the
component at 600 windows, five patterns, 100 million timing cells, one million
refinement cells, and 64 MiB working storage.

This exact legacy cadence does **not** tile the full dwell. It produces a dense
100 ms response curve but leaves 90% of samples outside the analyzed union.
Full-IQ tiling is a separate analysis contract and must not be described as
legacy parity.

### Measured benchmark

On `gauss` (Intel Core Ultra 9 285K, 24 physical cores), one J1 receiver/window
with Qin plus four surrogates took 1.439 s inside the analyzer; process wall time
was 1.57 s and maximum RSS was 48,616 KiB. The evidence itself accounted
149,985 timing cells, 940 refinement cells, and a 6.4 MB conservative working
bound.

Straight-line single-process extrapolation is about 14.4 minutes per receiver
or 28.8 minutes for two receivers over 60 s. Windows and receivers are
independent and can be dispatched across workers later, but this component does
not add orchestration. The prior Qin-only timing on the same J1 window was about
0.21-0.23 s, consistent with the five-pattern measurement.

Benchmark command shape:

```text
/usr/bin/time -f 'wall_s=%e max_rss_kib=%M user_s=%U system_s=%S' \
  uv run python <one-window native replay harness>
```

## Frozen external-corpus comparison

Fixture:
`tests/recording_analysis/fixtures/starlink_symbolwise_parity_2026_08_18_v1.json`
(`sha256:2fcafad31dd3a9be93ec27dea3884da46acbf20d134d6d0a4554e468175b575c`).
It pins object/window hashes, calibration-source hashes, explicit physical path
identities, receiver centers, and oracle statistics.

| Corpus / receiver | Center (Hz) | Epoch | Winning CFO (Hz) | Symbol margin | Conditioned margin | Native result |
|---|---:|---:|---:|---:|---:|---|
| RETRO 68.7 s / RX0 | 0.0 | 2063 | 364,150.848 | 0.105828 | 0.342091 | reproduced |
| RETRO 68.7 s / RX1 | 0.0 | 2063 | -194,343.874 | 0.122196 | 0.370500 | reproduced |
| J1 41.6 s / RX0 | 602,869.4 | 1193 | 446,568.534 | 0.085485 | 0.310992 | reproduced |
| J1 41.6 s / RX1 | 0.0 | 1193 | -165,350.377 | 0.108561 | 0.348625 | reproduced |

The external tests hash the complete IQ objects and calibration sources, hash
the exact 200,000-byte dual-receiver windows, run all five pattern searches,
and compare Qin fields with the frozen oracle.

## Irreducible numerical mismatch

The native port uses the already-published Redux Qin waveform. At 2.5 MS/s it
differs from the historical NumPy waveform in approximately 196 near-zero
components at roughly `1e-12`, due to Redux canonical zero handling. Timing,
epoch selection, phase-slope CFO, and symbolwise scores reproduce. The tiny
template difference propagates to conditioned full-frame scores at about
`2.7e-7`; external tests therefore use `5e-7` absolute tolerance for conditioned
scores and much tighter tolerances elsewhere. Changing the published Redux
template solely for bitwise oracle identity would be the wrong contract trade.

## Verification gates

Executed from the isolated worktree:

```text
uv run ruff check \
  src/leo_flow/contracts/starlink_symbolwise_replay.py \
  src/leo_flow/analysis/recording/starlink_symbolwise_replay.py \
  src/leo_flow/analysis/recording/__init__.py \
  tests/recording_analysis/test_starlink_symbolwise_replay.py \
  tests/recording_analysis/test_starlink_symbolwise_replay_external.py

uv run pytest -q \
  tests/recording_analysis/test_starlink_symbolwise_replay.py \
  tests/recording_analysis/test_starlink_symbolwise_replay_external.py
```

Result at report creation: `10 passed`; Ruff and focused mypy passed.
Adjacent template, surrogate-null, RETRO, and v0.3 acquisition suites added 31
passing tests. Source inspection also confirms that the native module contains
no `import leo_tracker` or `from leo_tracker` statement.

No dashboard, database migration, deployment file, live service, or live
recording was modified.
