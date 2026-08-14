# Starlink edge-pilot IF fixture

Status: offline generator qualified; conducted TX adapter not yet implemented

## Closed-loop software E2E

`tests/integration/test_starlink_closed_loop_e2e.py` now composes the real
module boundaries into one deterministic, hardware-free experiment:

| Stage | Evidence exercised |
|---|---|
| Generate | Three frozen backgrounds; one null and −12/−3/+9 dB nominal RX1 positives per background; both edges, inner/full pilot sets, three seeds, CFO, and paired-RX gain/phase/delay/noise; achieved SNR for both receivers is reported from fixture truth |
| Scan | The production eight-tuning center/order/rate/bandwidth geometry through `PlanCaptureEngine` and the metadata-aware V5 fake; 4,096 samples per segment are explicitly a fast test arm, not the production 262,144-sample duration |
| Store | SigMF local pair, verified V5 continuity, restart-safe spool publication, exact segment hashes, recording CAS objects, and a separate canonical truth object |
| Analyze | Typed recording-analysis job plus all three `IndependentDetectorSuite` methods; truth is absent from analysis dependencies |
| Carve | Byte-level background lineage is verified before related null/injections enter one split group; three groups are assigned, in time order, to TRAIN, VALIDATION, and LOCKED_TEST |
| Calibrate | Per-recording maxima from TRAIN only; balanced-accuracy selection with a deterministic higher-threshold tie break; rule identity binds exact training FeatureSets, labels, groups, methods, and policy |
| Evaluate | Durable recording-level confusion, segment-level false-alarm/miss denominators at the unchanged recording-calibrated rule, per-SNR target detection, firing covariance/phi, and coded-pilot frequency-support error; held-out +9 dB paired coherence must materially exceed its exact matched null |
| Report | Canonical evaluation and E2E summary objects in CAS, compact evaluation projection, and a successful `/api/evaluations/{eval_id}` dashboard response |

The whole pipeline runs twice and must reproduce the dataset digest, threshold
rule, evaluation digest/reference, summary bytes, and dashboard bytes exactly.
Run it with:

```text
.venv/bin/python -m pytest \
  tests/integration/test_starlink_closed_loop_e2e.py -q
```

The test deliberately reports detector failures as measurements rather than
requiring every method to look good. It asserts provenance, denominators,
frequency-support bounds, persistence, and reproducibility—not a desired
accuracy number. The 12 scans contain only three independent background groups,
and their 96 segment windows are not independent statistical trials.

Plan and recording IDs are opaque case numbers. Truth, signal presence, and
SNR remain only in the separately stored truth/control map and never become
analyzer dependencies or manifest identifiers.

The test reconstructs the filesystem CAS adapter and reopens recording, truth,
evaluation, and summary objects from their exact references. Its catalogs are
in-memory semantic fakes; PostgreSQL catalog durability, atomicity, and
read-only dashboard roles are covered by the separate PostgreSQL adapter
integration suite rather than hidden behind this hardware-free test.

The coded coefficients change every OFDM symbol. Therefore the rectangular
whole-segment coarse FFT is checked against the occupied support around the
nearest injected pilot center (half a 234,375 Hz subcarrier plus one FFT bin),
not misrepresented as a one-bin tone-center or CFO estimate.

## Purpose and boundary

This fixture gives detector development known digital truth while preserving
the three-part architecture:

| Concern | Owns | Must not own |
|---|---|---|
| Validation fixture | Published pilot geometry/codes, deterministic waveform, noise seed, level and truth digest | Radio discovery, capture plans, detector calls, dashboard publication |
| Capture | Receive-only recording and V5 continuity evidence | Knowledge of the fixture or expected detector result |
| Independent analysis | Optional injection into an immutable recording and per-recording feature extraction | Hardware TX state or cross-recording labels |
| Dataset/model analysis | Group-safe split assignment and scoring against injection truth | Regeneration of source IQ or inference that an unlabeled sky recording is negative |
| Future TX adapter | Explicitly armed conversion of one in-memory fixture to TX2 samples, bounded level ladder, mute/restore | Capture orchestration, scientific labels, persistence workflows |

The generator is `benchmark.starlink_pilot_if`. It has only standard-library
dependencies and imports no capture, analysis, dashboard, hardware, or legacy
runtime code. Its normal result is one in-memory CI16 byte string plus canonical
truth JSON. Explicit CLI output creates one waveform and one adjacent truth
record under an operator-selected local path; it never scans or writes NFS.

## What is modeled

[Qin et al.](https://arxiv.org/abs/2602.02627) publish 300 4QAM symbols for each
of 16 edge pilots. The fixture implements those Appendix A codes, the 4.4
microsecond OFDM symbol duration, the 750 Hz frame rate, and the following
geometry:

| Property | Value |
|---|---:|
| Subcarrier spacing | 234,375 Hz |
| Upper pilot indices | 488–495 |
| Lower pilot indices | 528–535 |
| Local offsets about either edge-band center | −820312.5, −585937.5, −351562.5, −117187.5, +117187.5, +351562.5, +585937.5, +820312.5 Hz |
| Eight-pilot occupied-bin width | 1,875,000 Hz |
| Lower edge-band offset from channel center | −115,429,687.5 Hz |
| Upper edge-band offset from channel center | +115,195,312.5 Hz |

This is a coded-pilot-only replica. It does not model the PSS/SSS, header,
payload, power loading from other subcarriers, transmitter impairments,
satellite motion, propagation, LNB response, or the complete Starlink downlink.
The truth record states that scope so a successful fixture test cannot be
silently promoted into an over-air claim.

The default random frame-phase mode deterministically changes the common phase
between frames. A cyclic TX buffer repeats its finite phase sequence at the
buffer duration, which must be recorded as a bench-fixture artifact rather than
treated as a measured Starlink property.

As a numerical-oracle check, coherent full-eight-pilot frames at 2.5 MS/s were
compared with the legacy implementation for both edges. Each 3,333-sample frame
matched within `2.39e-7` maximum complex magnitude error after conversion to
complex64. Redux tests separately freeze the complete 16-code table digest,
selected symbol prefixes, geometry, and independently reconstructed sample
math; Redux never imports the legacy repository at runtime.

## Useful fixture choices

| Goal | Pilot indices | Minimum useful rate | Comment |
|---|---|---:|---|
| Minimal loopback | lower 531,532 or upper 491,492 | 1.25 MS/s | Inner pair at ±117187.5 Hz; fastest safe bring-up |
| Four-pilot structure | lower 530–533 or upper 490–493 | 1.25 MS/s | Centers extend to ±351562.5 Hz |
| Full published edge set | lower 528–535 or upper 488–495 | 2.5 MS/s | 1.25 MS/s is rejected because the outer centers exceed Nyquist |

For channel 4 with a 9.75 GHz LNB-equivalent plan, the lower edge-pilot IF
center is 1,709,687,500 Hz. A lower inner-pair fixture therefore places its
centers at 1,709,570,312.5 and 1,709,804,687.5 Hz. TX2 and RX should both use
the edge-band center as their LO in the conducted fixture. The previous V5
canary center, 1,825,117,187.5 Hz, is the channel center and is not the right LO
for a narrow edge-pilot recording.

## Real-scan compatibility check

The archive at `/mnt/qnap01/mouse9911/leo-scans` is an empirical background,
not truth. Its corrected cross-radio report withdrew the earlier detection
claims because highly correlated methods also passed the negative control.
Exact digital injection supplies the missing known-presence label; the base
recording still must be digest-bound and must remain in one split group with all
of its injections.

Read-only validation used this committed archive entry:

| Field | Value |
|---|---|
| Sweep | `sync-20260814T144838Z` |
| Radio / receiver | `pluto-19f2` / `lnb-c` |
| Tuning | channel 4 lower, tuning index 7 from that radio's declared order |
| Shape | `[8, 400000, 2, 2]` CI16 |
| Rate / duration | 5 MS/s / 80 ms |
| Background complex RMS | 554.75 counts |
| Background component peak | 2048 counts |

A two-pilot `(531,532)` waveform with seed `20260814` and random per-frame
phase was added only in memory. The normalized exact-replica match and clipping
audit were:

| Fixture RMS counts | Signal/background over coded samples | Background match | Injected match | Added clipping |
|---:|---:|---:|---:|---:|
| 16 | −30.725 dB | 0.002546 | 0.027427 | 0 |
| 32 | −24.702 dB | 0.002488 | 0.056491 | 0 |
| 64 | −18.684 dB | 0.002491 | 0.113967 | 0 |
| 128 | −12.663 dB | 0.002508 | 0.225125 | 1 |

These numbers validate layout, sample-rate geometry, deterministic replica
matching, and the need to audit clipping after injection. They do not prove the
background is empty or qualify a detector threshold. A first offline injection
sweep should use several frozen backgrounds and levels around −32 to −12 dB,
record actual clipping, and retain an uninjected copy as the paired control.

## Generated example

Generate a three-frame, two-pilot fixture in local temporary storage:

```text
python3 -m benchmark.starlink_pilot_if \
  --sample-rate 5000000 --sample-count 20000 \
  --edge lower --pilots 531,532 \
  --signal-rms 128 --seed 20260814 \
  --if-center-hz 1709687500 \
  --output /tmp/starlink-lower-inner-pair.ci16
```

Add `--noise-snr-db -12` to include deterministic, RMS-normalized digital
uniform-component noise. For real-noise injection, omit that option and scale
the replica against the selected immutable background. Digital noise sent
through TX2 is common to both tee branches; it is not independent receiver
noise.

## Conducted TX2 gate

No current command transmits this waveform. The next hardware slice should be
small and separately reviewed:

1. Verify the radio serial and tee/attenuator topology; fail if TX2 is not
   explicitly armed for that serial.
2. Set both transmit paths to −80 dB and disable every DDS/cyclic buffer before
   loading samples.
3. Tune TX2 and both receivers to the selected pilot-band IF center, then arm a
   single finite/cyclic fixture only after readback.
4. Start with the inner two pilots at 16 RMS counts and −80 dB TX attenuation.
   Increase one bounded step at a time while measuring RX RMS, pilot contrast,
   and clipping independently on RX1 and RX2.
5. Stop on any clipping, unexpected LO/gain readback, continuity gap, or missing
   metadata. Always mute TX2 and restore the documented receive state in a
   `finally` path.
6. Persist one capture object plus its fixture truth and hardware evidence in
   CAS/catalog storage. Do not create a per-refill or per-frame file tree.

Only after the two-pilot path passes should the ladder expand to four and then
eight pilots, CFO offsets, digital-noise levels, and randomized frame occupancy.
