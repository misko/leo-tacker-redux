# Benchmark corpus design and Wave 0 inventory

## Purpose

The benchmark is a reproducible index over immutable legacy recordings and
future controlled truth. It must let recording-analysis agents compare
algorithms without importing the legacy repository, copying tens of gigabytes,
randomly resampling a moving corpus, or learning labels from the method they
are evaluating.

The source of record is
`benchmark/manifests/development-2026-08-13.json`. Its nine members reference
about 14.1 GB of available IQ while adding none of those bytes to Git. Each
reference is locked by the source-manifest hash, payload-entry index hash,
exact byte total, legacy analysis-report hash, and follow-up-report hash. The
current QNAP locations are hints resolved through root IDs; moving a byte-
identical object does not alter membership.

## Initial development selection

This intentionally small engineering set contains:

| Coverage | Included |
|---|---|
| Radios / chains | `pluto-5d4d` (`lnb-a`, `lnb-b`) and `pluto-19f2` (`lnb-c`, `lnb-d`) |
| Observation modes | narrow, oversample, wide, and a retained channel-hop segment |
| Gain | manual 50 dB and `slow_attack` AGC |
| Tuning | channels 1–4 and both upper/lower edge regions |
| Hardware epochs | current gain-series-v4 firmware on both radios and an older fingerprint-v2 hop capture |
| Legacy outcomes | same-receiver proxy confirmations, candidates, no-confirmation outputs, receiver-power imbalance, near-full-scale activity, and evidence-only retention |
| Physical availability | eight complete legacy raw sets and one 2 MB deterministic retained clip whose original 40 MB hop raw is no longer present |

The selection is bounded and diagnostic, not statistically representative.
All 2026-08-13 recordings share one conservative split group even when they
come from different radios. The 2026-08-10 hop is another group. Therefore this
manifest is a development fixture only; it does not create a credible random
train/validation split from a few neighboring recordings.

## Leakage-resistant dataset construction

Build future datasets in this order:

1. Assign the coarsest correlation boundary first: station, contiguous capture
   session, UTC day, and independently established pass group. Every recording,
   segment, window, injection based on that noise, and later FeatureSet stays
   with that group.
2. Allocate whole groups to time-ordered train, validation, and locked test.
   Use half-open time intervals and freeze the explicit membership list and
   digest; never reroll a percentage sampler.
3. For radio/LNB generalization claims, create an additional evaluation whose
   locked test holds out complete receiver-chain and hardware validity epochs.
4. Fit thresholds and calibration on training only, choose methods on
   validation, and open locked results only after configuration, code digest,
   metrics, and pass/fail bounds are frozen.
5. A global model consumes frozen FeatureSet IDs from the same membership. It
   must not feed a model fitted on validation/test data back into those
   recordings and report the result as independent performance.

Pass IDs are currently unknown for the Wave 0 entries and explicitly null.
Before any pass-continuity claim, establish them from independent temporal
grouping without using the detector score being evaluated.

## Labels and ground truth

The manifest preserves label provenance and the six tiers in
`benchmark/README.md`. Three initial entries are legacy tier-5 proxy positives;
all others are tier-6 unlabeled. In particular:

- “confirmed” currently means a same-receiver follow-up link for these
  examples; none is dual- or cross-receiver confirmed;
- “candidate”, “analyzed”, or no legacy confirmation is not a negative;
- the frozen legacy oracle is only a regression observation;
- no TLE association is promoted to truth.

Future digital injections must be made into a frozen real-noise recording and
carry both the base recording hash and the independently generated injection
spec/hash. The base noise and every derived injection remain in one split
group. Hardware loopback/RF labels must reference independent signal-generator
settings and measurement records. Independent external evidence must identify
the instrument and prove it was not produced by the detector under test.

## Synthetic fixture

`benchmark/specs/synthetic-iq-v1.json` supplies three small, materializable
fixtures: a heavily clipped chirp, a clean two-receiver chirp, and a weak static
tone. It uses an integer phase accumulator, deterministic integer noise, exact
receiver delays/gains, explicit quantization, and normative CI16 hashes. This
keeps the fixture independent of NumPy, SDR code, and detector kernels.

The synthetic oscillator is deliberately generic. Add a separate waveform
fixture only after its definition is independently reviewed; never implement a
“synthetic truth” generator by calling the production detector or model.

## Frozen legacy oracle

`benchmark/oracles/development-2026-08-13.legacy-summary.json` freezes eight
integer/boolean analysis summary fields and four follow-up confirmation fields
per member. Every entry repeats the exact source report hashes and is joined to
the manifest by `legacy_oracle_entry_id` and membership digest. Tests must not
regenerate this file from the legacy repository when a comparison fails.

Parity is required only for a numerical kernel intentionally ported with an
approved tolerance. Filenames, report trees, orchestration behavior, thresholds,
and incidental ordering are outside the oracle.

## Gaps before scientific promotion

The development corpus deliberately fails `--promotion-gate`. At minimum add:

- exact digital-injection positives spanning SNR, drift, frequency offset,
  delay, gain, clipping, and real-noise backgrounds;
- controlled hardware/RF truth and independent external positives;
- hard nulls selected without looking at the candidate detector;
- known real interference and independently characterized confounders;
- interrupted and corrupt source fixtures for failure behavior;
- multiple disjoint days and complete pass groups;
- temperature range, LNB swaps, and at least one held-out receiver/hardware
  epoch for any cross-hardware claim;
- enough independent null hypotheses to place confidence intervals on false
  alarms, plus an untouched locked-test manifest held by the release steward.

Station identity and pass grouping in the legacy files also need independent
verification. Until these gaps close, this corpus supports interface work,
determinism, legacy parity, and exploratory comparison—not detector accuracy,
association accuracy, or satellite-tracking claims.
