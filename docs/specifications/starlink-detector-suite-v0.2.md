# Starlink detector suite v0.2

Status: implementation specification

This specification ports the scientifically relevant methods evaluated in
`leo-tracker` without making that repository a runtime dependency.  It is based
on the complete 2026-08-15 detector evaluation, its linked corrected
cross-radio report and detector plan, and Qin et al., arXiv:2602.02627v1.

The suite produces candidate evidence.  It does not produce a detection bit or
a beacon count.  Those require an exact-profile threshold fitted to independent
training null searches, accepted on a disjoint holdout set at a declared
whole-search false-alarm rate, followed by event clustering.

## Method matrix

| Method | Role | Redux disposition | Search and conditioning | Output |
|---|---|---|---|---|
| `coarse-A` (3x8, +/-300 kHz) | historical proposer | rejected for new production work | none; retained only in the evidence matrix | reason: spacing/span are superseded by measured config E |
| `coarse-E` (13x8, +/-700 kHz) | candidate proposer | supporting | lag x declared CFO bank; top-K proposal only | epoch, CFO, score, search cells and support; never a verdict |
| `pss-sss-lag-doppler` | Qin/Humphreys coarse frame acquisition | supporting, bandwidth-gated | lag x Doppler bank using an externally pinned PSS+SSS template; candidate maximum reproduced at the winner | score, lag, Doppler, support and complete searched identity |
| `anchor-8` | report confirmer | portable but exploratory | exact score searches declared epoch/CFO cells; its roll-17 control is evaluated only at the exact winner | searched score, conditioned exact/control, margin, per-frame mean/max/support |
| `differential-16` | report confirmer | portable | same outer search; 16 contiguous symbols; adjacent products cancel frame phase | score, continuous residual CFO, conditioned roll control, frame summaries |
| `differential-32` | report confirmer | portable | as above with 32 contiguous symbols | same evidence shape |
| `glrt-32` | report confirmer and leading prior | portable | same outer search plus a declared residual-CFO grid over one ambiguity period; the control is evaluated at the exact winner, never maximized independently | score, residual CFO, conditioned control and complete cell count |
| `glrt-64` | report confirmer | portable | as above with 64 contiguous symbols | same evidence shape |
| `full-frame-acquire` | withheld-symbol block | portable | conditioned/search score using only the declared ACQUIRE set | score and frame summaries; symbol set is explicit |
| `full-frame-verify` | withheld-symbol block | portable verifier | evaluated on a symbol set disjoint from ACQUIRE | score and frame summaries plus a machine-checked disjointness claim |
| `full-frame-full` | report confirmer | portable reference | union of ACQUIRE and VERIFY, evaluated under the same search identity | score and frame summaries |
| `full-frame-300` | historical exhaustive proposer | supporting reference only | exhaustive epoch search is allowed only under explicit resource bounds; not the default acquisition path | candidate evidence, never a production threshold shortcut |
| exact roll-17 code | wrong-code control | supporting, mandatory | fixed to the target method's exact winning epoch, coarse CFO and residual CFO | conditioned score and exact-minus-control margin |
| searched roll-17 code | proposed same-tuning null | rejected | a free epoch re-acquires the real waveform shifted by 17 symbols | explicit rejection reason; never calibration input |
| opposite-edge score at target-selected points | proposed null | rejected | selection differs from the target statistic | explicit rejection reason; never calibration input |
| independently searched cross-edge arm | target-code-free null | portable calibration input | runs the complete target search on its own cells | one maximum per whole-search trial |
| per-frame mean | temporal combiner | portable descriptive statistic | mean of normalized per-frame scores | finite bounded value plus support |
| per-frame maximum | sparse-occupancy combiner | exploratory | maximum over the same per-frame scores | finite bounded value plus support; separately calibrated if promoted |
| all-pair-32 | relative-phase variant | exploratory | all symbol-pair phase products rather than adjacent products | research score only; no default dashboard verdict |
| phase-only weighting | weighting variant | rejected as a default | normalizes each correlation magnitude before combining | retained only as a declared experimental variant |
| per-subcarrier differential/equalization | receiver compensation | rejected | measured benefit was 0.0--0.4 dB | no implementation in the production suite |
| multiple timing candidates / epoch dithering | acquisition variants | rejected as primary methods | report found no promise; they may only expand a declared experimental bank | no hidden fallback |
| periodicity-only detector | primary detector proposal | rejected | periodicity is supporting structure, not Starlink-code evidence | no candidate or verdict |
| PSS/SSS-only detection at narrow edge bandwidth | acquisition variant | rejected as a primary detector | the 2.5 MHz edge view contains about 1% of its energy | may emit supporting evidence only and must state bandwidth limitation |
| 1.25 MS/s pilot analysis | clipped pilot stratum | separated | the 1.875 MHz edge-pilot allocation does not fit | `clipped-pilot-band`; never pooled with full-band strata |
| two-radio coincidence | corroboration | portable after calibrated per-stream decisions | noncoherent time/channel/edge/CFO compatibility; no phase fusion | software-coordinated multi-radio evidence, never hardware-sync language |
| coincidence occupancy solver | population inference | rejected for beacon verdicts | assumptions fail on heterogeneous sky data and its old consistency check is vacuous | research-only population output, never a per-capture detection |
| shifted and scrambled joins | negative controls | portable evaluation checks | rerun the same population analysis on definitionally false joins | diagnostic evidence, never a detector vote |
| injected Qin waveform | ground truth | mandatory evaluation harness | seeded amplitude/SNR, CFO, epoch, occupancy and drift; null and positive schedules are disjoint | scores, Pd at fixed whole-search FAR, confidence and provenance |
| receiver absolute-centre correction | acquisition configuration | supporting, mandatory provenance | epoch-bound per-receiver measurement, never an epoch-blind fleet constant | hardware/search profile identity |
| TLE/Doppler association | post-detection attribution | deferred/separate | operates on clustered calibrated events | satellite association or `not_evaluated`; never detection truth |
| T-codes / other low-entropy elements | full-band PNT method | out of scope for edge captures | needs the full 240 MHz waveform/all subcarriers | not implemented by this edge-pilot suite |

## Public v0.2 evidence contract

The new contract is additive and has a distinct schema version; existing v0.1
objects are not mutated.

Inputs to one pure analysis are:

- immutable recording, segment and receiver identities;
- finite complex samples and their exact sample rate;
- edge and Qin exact/roll-17 template references;
- an externally pinned PSS+SSS template reference when supporting acquisition
  is requested;
- the complete epoch, coarse-CFO and residual-CFO hypothesis banks;
- an explicit interleaved ACQUIRE/VERIFY symbol split;
- maximum samples, search cells, emitted methods and frame summaries.

Each method result records:

- method identifier and algorithm/config/template digests;
- searched versus conditioned statistic identities;
- declared and effective search-cell counts;
- winning epoch, coarse CFO and residual CFO;
- searched exact score and its conditioned reproduction;
- roll-17 score at that exact same cell and the margin;
- per-frame mean, maximum and support;
- exact pilot symbols used and, for full-frame blocks, split identity;
- warnings including clipped-band and supporting-only states.

The bundle records the ordered method set, a suite identity digest, a sampling
stratum (`full-pilot-band` or `clipped-pilot-band`), optional PSS/SSS evidence,
and `candidates_only = true`.  It has no threshold, detection boolean or beacon
count.

## Statistical identities and invariants

1. A searched statistic and a conditioned statistic are distinct identities.
2. The exact winner must reproduce under conditioning to numerical tolerance.
3. Roll-17 controls use the exact winner's epoch and all CFO coordinates.  An
   independently searched roll is forbidden.
4. Calibration trials are maxima from complete searches, not candidate points.
5. ACQUIRE and VERIFY symbols are non-empty, sorted, unique and disjoint; FULL
   is their exact union.
6. Frame amplitudes are never summed coherently across frames.  Magnitudes,
   normalized energies or phase-cancelled products may be combined.
7. Every array and emitted collection is bounded before analysis begins.
8. Every hardware, template, algorithm, configuration, dataset and split
   identity is content-addressed.
9. A 1.25 MS/s result cannot share a calibration cell with a rate at which the
   1.875 MHz pilot allocation fits.
10. Multi-radio evidence is noncoherent and named
    `software-coordinated-multi-radio`; measured first-sample skew is evidence,
    not a claim of hardware synchronization.

## Required test cases

- numerical oracle vectors produced once from `leo-tracker` for all eight
  confirmer methods, both edges, exact and roll-17 templates;
- noiseless known answers (score 1 where defined) and exact winner
  reproduction;
- gain, global phase and frame-period translation invariance;
- positive and negative CFO sign tests and fractional 3333.333-frame cadence;
- ACQUIRE/VERIFY disjointness and a mutation test proving VERIFY changes do not
  change ACQUIRE;
- a test proving a searched roll can re-acquire a shifted exact waveform and
  is therefore rejected, while same-cell conditioning suppresses it;
- whole-search trial accounting and rejection of per-point thresholds;
- deterministic seeded null and injection schedules spanning SNR, CFO, epoch,
  occupancy and drift;
- per-frame mean/max/support on sparse occupancy;
- strict sample, cell, method and output bounds;
- 1.25 MS/s clipped-stratum separation;
- two-radio clustering that corroborates without phase fusion, duplicate
  counting or hardware-sync claims;
- no runtime import from `leo_tracker`.

## Reference conclusions carried forward

At a common 5% per-cell false-alarm rate the report supports an unordered
leading group `{full-frame-verify, full-frame-full, glrt-32,
full-frame-acquire, glrt-64}`, followed by `{anchor-8, differential-32}`, then
`differential-16` under the measured 20 ms / 5 MS/s loopback condition.  The
ranking changes with arm and must not be hardcoded as a universal order.

Cross-detector agreement is not independent corroboration: binary decisions
were correlated at phi 0.841--0.946.  Dashboard and event logic must therefore
show the methods as a comparison suite, not count them as separate beacons.
