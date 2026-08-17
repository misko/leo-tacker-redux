# Recommended continuous `.20`/`.21` schedule

Status: implemented and tested in the offline main-policy/deployment sources,
but not materialized, armed, or installed. This work did not open a radio or
modify live services, databases, campaign state, or object storage.

## Recommendation

Use a finite, capture-first eight-hour campaign with **936 coordinated capture
slots** on the exact rational period **400/13 seconds** (30.769230769… s).
After capture closes successfully, start a separate local analysis drain that
publishes FeatureSet, waterfall, and calibrated Starlink candidate projections
to the dashboard.

This is the fastest schedule recommended from the v6 evidence without changing
the 15 s preflight contract. It is 4.33× denser than the current 133.333 s grid,
leaves 6.04 s between the slowest observed publication and the next preflight
boundary, and fits exactly 26 balanced 36-slot supercycles into eight hours.

Do not launch the theoretical 25 s grid as the first production schedule. Its
margin is only 0.274 s after the slowest v6 capture/publication before the next
15 s preflight window. It is a useful later canary target, not a defensible
starting safety margin—especially after v6 observed a 150.687 ms skew failure
even with a long idle interval.

## Measured v6 basis

The immutable terminal audit has SHA-256
`465a1a28ac221e8c802cca9eafb17f738df4ac65236078c2148b588b1d3b1ff7`.
Timing below is measured on the real `.20`/`.21` IP path. “Analysis” is the v6
ordinary feature/waterfall pair analysis; v6 explicitly records that the
Starlink campaign pipeline was not wired.

| Cell | Rate × dwell | Pair capture wall | Requested start → both published | Slowest last-segment → publication | Pair analysis |
|---:|---|---:|---:|---:|---:|
| u000 | 1.25 MS/s × 40 ms | 6.522 s | 6.532 s | 0.178 s | 9.635 s |
| u001 | 1.25 MS/s × 80 ms | 7.249 s | 7.259 s | 0.238 s | 11.544 s |
| u002 | 1.25 MS/s × 160 ms | 9.479 s | 9.490 s | 0.446 s | 15.490 s |
| u003 | 2.5 MS/s × 40 ms | 5.417 s | 5.427 s | 0.199 s | 11.532 s |
| u004 | 2.5 MS/s × 80 ms | 6.691 s | 6.701 s | 0.321 s | 15.477 s |
| u005 | 2.5 MS/s × 160 ms | 9.096 s | 9.105 s | 0.534 s | 24.346 s |
| u006 | 5 MS/s × 40 ms | 5.957 s | 5.967 s | 0.287 s | 15.488 s |
| u007 | 5 MS/s × 80 ms | 6.690 s | 6.695 s | 0.468 s | 24.250 s |
| u008 | 5 MS/s × 160 ms | **9.715 s** | **9.726 s** | **0.869 s** | not invoked after skew failure |

Across the eight analyzed cells, pair analysis took 9.635–24.346 s (median
15.482 s, mean 15.970 s). These nine points are an engineering envelope, not a
population percentile. The production readiness test must collect a longer
canary distribution and separately benchmark the Starlink stage.

## Balanced 36-slot supercycle

Each recording still scans all eight channel/edge tunings. The nine-cell
rate/duration matrix is indexed by `success_index mod 9`. Independently, use
`slot_index mod 4` for cross-radio edge geometry:

| Geometry phase | Radio `.20` order | Radio `.21` order | Simultaneous purpose |
|---:|---|---|---|
| 0 | L | L | same-edge replication, lower first |
| 1 | L | U | opposite-edge diversity, `.20` lower first |
| 2 | U | U | same-edge replication, upper first |
| 3 | U | L | opposite-edge diversity, `.21` lower first |

Because 9 and 4 are coprime, 36 slots cover every rate/duration cell under every
geometry exactly once. In 936 slots, each combination appears 26 times. This
provides equal same-edge/opposite-edge exposure, equal `.20`/`.21` first-edge
assignment, and equal rate/duration representation without random scheduling.

The 1.25 MS/s cells remain deliberately clipped: 625 kHz of the 1.875 MHz pilot
band lies outside the sampled bandwidth. Keep them for rate characterization,
tag them as clipped, and never pool them with complete-pilot 2.5/5 MS/s detector
statistics.

## Cadence, duty, and storage comparison

RF duty is actual sample integration time: eight dwells per recording. “Capture
process occupancy” uses the sum of measured v6 pair wall times and therefore
includes tuning, setup, and publication overhead. Storage is raw sample payload
for both radios; the capacity gate must additionally reserve 2× remaining raw
bytes plus the existing 10 GiB margin.

| Schedule | Slots / 8 h | Balanced supercycles | RF duty / radio | Measured process occupancy | Raw / 8 h | Average raw rate | Admission reserve at start |
|---|---:|---:|---:|---:|---:|---:|---:|
| Current 133.333 s | 216 | 6 | 0.560% | 5.568% | 7.5264 GB (7.1 GiB) | 0.261 MB/s | 25.790 GB (25 GiB) |
| **Recommended 400/13 s** | **936** | **26** | **2.427%** | **24.128%** | **32.6144 GB (31 GiB)** | **1.132 MB/s** | **75.966 GB (71 GiB)** |
| Experimental 25 s | 1,152 | 32 | 2.987% | 29.696% | 40.1408 GB (38 GiB) | 1.394 MB/s | 91.019 GB (85 GiB) |

At audit time, the filesystem holding state and objects reported
910,993,367,040 bytes available (849 GiB), so the recommended initial admission
reserve fits by a wide margin. Capacity is time-varying: the service must still
measure it immediately before arming and before every remaining-work decision.
The reserve is not a retention policy; define retention/export separately.

The current scan spends far more time changing LO and opening/publishing a
recording than collecting samples. Increasing dwell would raise true RF duty
more efficiently than reducing cadence, but it would change the reviewed
rate/duration experiment. Do that only as a separately qualified science
profile.

## Timing and failure policy

- Preserve process isolation: one child process per radio and no shared live
  libiio context.
- Preserve a common requested UTC release and measure actual first sample on
  both radios. This remains software coordination, never hardware sync.
- Keep the 100 ms observed first-sample skew gate. Preserve both successful
  recording objects if it fails, mark the pair ineligible, and halt the
  coordinated campaign without catch-up.
- Keep 15 s preflight lead and 5 s maximum start lateness. The 400/13 s cadence
  yields `30.769 − 15 − 9.726 = 6.044 s` measured envelope margin.
- Never move a missed slot forward. A missed slot is a durable failure requiring
  a new future campaign identity, not a compressed burst.
- Do not run local analysis while the capture phase owns the campaign. The
  dashboard may show capture rows immediately; derived products appear during
  the post-capture drain.

## Service and transition requirements

Use only the two-phase commands:

1. `capture-run` owns radios and advances capture until it durably changes
   `CAPTURING → ANALYZING`.
2. `drain-analysis` starts only through capture service `OnSuccess`, has no radio
   ownership, and advances all captured batches before durably changing
   `ANALYZING → COMPLETE`.

The legacy combined `run` deployment is not suitable. With 216 slots and the
current 433-call bound, NOT_DUE iterations can consume the whole budget just as
capture closes, leaving no analysis calls even though the oneshot exits zero.
The obsolete `deploy/gauss-continuous-v1` service and private continuous `run`
command have been removed; the qualification-specific campaign runner remains
separate.

Loop-call bounds must account for NOT_DUE calls, not merely successful captures:

| Command | Exact calls without NOT_DUE | Worst bounded calls with one NOT_DUE per slot | Recommended 936-slot bound |
|---|---:|---:|---:|
| `capture-run` | N captures + 1 phase close | 2N capture/not-due calls + 1 phase close | **1,873** |
| `drain-analysis` | N analyses + 1 phase close | same; it does not wait on RF slots | **937** |
| legacy combined `run` | 2N + 2 | up to 3N + 2 | remove; 2,810 for N=936 |

The reviewed v3 definition now derives and encodes the exact 1,873/937 limits,
and the operator rejects a conflicting command-line value. The bounds remain
finite. `capture-run` has enough budget to finish the whole
RF phase: if it exhausts immediately after a NOT_DUE wait, retryable exit 91 plus
the current 30 s `RestartSec` would miss a 30.769 s slot. Unexpected retryable
slice exits can still restart, but use a restart delay proven smaller than the
remaining slot/lateness margin.

For `drain-analysis`, exit 91 is safely restartable because there is no RF
deadline and the journal is durable. Exit 4 remains fail-closed and must stay in
`RestartPreventExitStatus`; it must not trigger `OnSuccess`. With 936 batches,
ordinary v6 analysis extrapolates to about 4.15 h at the observed mean or 6.33 h
if every batch equals the observed maximum. A 9 h analysis slice permits an
average 34.6 s per pair, about 10.3 s above the ordinary observed maximum for
Starlink work. This is only a capacity target: benchmark the wired Starlink
pipeline before setting its service deadline.

## Implementation sequence and required tests

Steps 1–5 are implemented and covered by offline tests. Steps 6–8 remain
deliberately unexecuted live work.

| Order | Change | Required proof |
|---:|---|---|
| 1 | Add a versioned schedule policy carrying rational period, target count, and 4-phase geometry | codec round-trip/golden contract; reject zero/non-integral or non-8h schedules; do not alter the published v2 contract |
| 2 | Generalize target, raw-byte budget, deadline, and transition caps from the immutable definition | unit tests for N=936, exact 32,614,400,000 raw bytes, 75,966,218,240-byte admission requirement, and overflow/bounds rejection |
| 3 | Materialize the 36-slot cross-product | tests prove each of 9 cells × 4 geometries appears exactly 26 times; both radios still have eight valid tunings |
| 4 | Keep capture and analysis services separate; remove the legacy combined deployment | service tests prove only capture has radio/runtime libraries, analysis starts only on capture success, exit 4 does not restart, exit 91 does |
| 5 | Correct transition sizing | state-machine tests exercise early invocation, one NOT_DUE per slot, exact 1,873/937 closure calls, exhaustion at NOT_DUE, restart, and resume without duplicate identity |
| 6 | Run a read-only/dry materialization, then a short 36-slot canary at 400/13 s | no missed slots, 36 terminal pairs, every skew <100 ms, zero TX/DDS/constant-IQ/continuity failures, observed publication <15.769 s after requested start |
| 7 | Benchmark deferred ordinary + Starlink analysis on the canary | immutable results for all 72 recordings, calibrated/not-evaluated semantics, pair mean and maximum below the selected drain budget, dashboard rows/details/waterfalls without 5xx |
| 8 | Arm the finite 936-slot campaign from exact digests | fresh capacity ≥75,966,218,240 bytes, drain/inactive/start gates true, no other radio or analysis owner, operator rollback/stop procedure recorded |

Promotion to the 25 s profile requires a separate canary demonstrating enough
margin across the slowest cells. A reasonable gate is at least 1,000 consecutive
terminal pairs, zero misses, zero overlap, every skew under 100 ms, and a
measured high-tail publication time that leaves a reviewed preflight margin.
Do not infer that gate from the nine-cell v6 sample.
