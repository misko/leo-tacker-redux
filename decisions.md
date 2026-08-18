# Decisions

## 2026-08-17 — Full-coverage spectrogram and blind Doppler evidence

**Status:** accepted for additive analysis and dashboard integration; candidate
evidence only, not a calibrated signal or Starlink-beacon decision.

### Decision

Redux will retain waterfall v0.1 unchanged and add the independent
`org.leo-flow.waterfall-bundle/v0.2` product for long-dwell visualization and
blind motion analysis. The v0.2 producer processes every complete,
non-overlapping 32,768-sample FFT frame in each verified contiguous RF span and
records exact analyzed samples, discarded tails, frame starts, span counts, and
coverage fraction. Its bounded display product contains 512 frequency bins and
up to 200 temporal rows per tile, with three separately selectable layers:

- linear-power mean converted to dB;
- per-frequency temporal-median residual in dB; and
- nearest-rank 95th-percentile linear power converted to dB.

The blind Doppler tracker consumes a producer-neutral spectrogram slice through
a narrow public port. The integration adapter selects the v0.2
`temporal_median_residual_db` layer and preserves the exact waterfall input
identity, segment, receiver, frequency axis, time axis, and power-reference
label. Analysis code does not import dashboard or persistence implementations,
and the waterfall producer does not import the tracker.

The basic tracker extracts bounded sub-bin peaks, links them with explicit
time/frequency continuity and gap limits, fits robust constant, linear, and
quadratic paths, and publishes only a bounded top-K candidate set. Advanced
evidence may add held-out de-Doppler scores, stationary/opposite/time-shuffle
controls, multi-track peeling, held-out comb support, broadband edge/texture
motion, and dual-receiver common-motion evidence. Missing advanced inputs are
represented as unavailable evidence; they are never synthesized.

TLE association is a separate post-blind step. The immutable blind-evidence
identity must be closed before ephemeris candidates are introduced, ambiguous
matches remain explicit, and no TLE match may retroactively change the blind
candidate or its controls.

### Dashboard semantics

The recording view and API will expose one selected waterfall layer at a time
(`average`, `residual`, or `high-percentile`), overlay the bounded Doppler paths,
and show frequency, time, drift rate, acceleration, duration, SNR, residual,
coverage, and stationary-control facts. Layer changes fetch a separately bounded
projection; the API does not duplicate all three durable matrices in one response.
The UI must say
**candidate Doppler evidence** and must not call a ridge, threshold crossing, or
TLE association a confirmed Starlink beacon. Existing waterfall v0.1 and V1–V5
dashboard routes remain immutable; the new view is additive.

### Promotion gates

Production promotion requires all of the following:

- exact codec round trips and immutable replay/conflict rejection;
- component tests for stationary, positive/negative linear, quadratic,
  intermittent, crossing, broadband, edge-truncated, AGC-step, and noise cases;
- a real-recording back-process proving bounded runtime, memory, coverage, and
  cancellation behavior;
- dashboard browser tests for all layers, overlays, empty evidence, malformed
  evidence, and candidate-only language;
- independently calibrated null and injected-signal corpora before any
  detection threshold is introduced; and
- integration-steward review of migration, deployment, dependency, and release
  changes.

### Reference boundary

`leo-tracker` revision
`0bb80d14759fd8496b74e7d3219a690be18565a6` remains a numerical oracle only.
Redux may freeze derived test expectations with provenance, but no Redux runtime
module imports or executes `leo-tracker`.

## 2026-08-17 — Provisional reproduction of the leo-tracker report fire rule

**Status:** accepted for historical comparison and compatible Redux back-processing;
not approved as a calibrated Starlink-beacon detector.

### Decision

Redux will provisionally reproduce the leo-tracker report-era rule for inputs with
the exact report score semantics and an exact overlapping sample-rate/probe cell.
For each method, one observation is a candidate fire when the maximum score over
the report's distinct candidate points is **strictly greater than** the frozen
`(method, sample rate, probe duration)` threshold.

All public output must call this a **provisional report-era candidate fire** (or
fire rate), never a calibrated detection, beacon detection, or detection rate.
The additive `org.leo-flow.provisional-report-era-fire-decision/v0.1` contract
preserves that distinction. No existing public contract is changed.

The reviewed threshold reconstruction is vendored as an immutable, Redux-owned
artifact at
`src/leo_flow/analysis/recording/artifacts/report_era_thresholds_v1.json`.
Runtime code reads only this packaged Redux artifact; leo-tracker and the operator
evidence directory remain reference/numerical-oracle inputs and are not runtime
dependencies.

### Enabled compatibility cells

Only the current-corpus dimensions proven to overlap exactly are enabled:

| Sample rate | Probe | Probe samples | Status |
|---:|---:|---:|---|
| 2.5 MS/s | 80 ms | 200,000 | provisional report-era rule enabled |
| 2.5 MS/s | 160 ms | 400,000 | provisional report-era rule enabled |
| 5 MS/s | 80 ms | 400,000 | provisional report-era rule enabled |
| 5 MS/s | 160 ms | 800,000 | provisional report-era rule enabled |

The caller must also supply the pinned report score identity
`report-era-fcore-build/distinct-candidate-point-maximum/v1@0bb80d14759fd8496b74e7d3219a690be18565a6`.
Dimensions alone are insufficient. A different algorithm, candidate grid, point
deduplication rule, maximum-selection rule, or score identity returns an explicit
`not-applicable` result without a threshold or fire verdict.

### Unsupported cases and fail-closed behavior

- Deployed Redux v0.2 uses 8 ms prefixes and different search geometry. It remains
  unclassified; report thresholds are not attached to those results.
- Current 40 ms captures have no frozen report threshold.
- The report has 640 ms thresholds, but the current comparison corpus has no
  640 ms captures. Those artifact rows remain audit evidence and are not enabled.
- The report has 1.25 MS/s thresholds, but that rate clips the 1.875 MHz pilot
  band. It is not enabled.
- The report has 10 MS/s thresholds, but the current comparison corpus has no
  10 MS/s captures. It is not enabled.
- No value is interpolated, extrapolated, pooled across dimensions, or substituted
  for a missing cell.

### Rationale and limitations

This gives current back-processing a faithful comparison with the earlier report
while preserving the semantic boundary between a historical threshold crossing
and a confirmed beacon. The report fitted a nominal 1% order statistic to
individual cross-edge null-arm point scores, then applied it to each observation's
maximum over many searched points. Search multiplicity raised observed whole-window
control fire rates to **5.47%–6.74%**, depending on method. The rule therefore does
not provide a 1% whole-search false-alarm rate.

The report sky corpus has no independent signal-present/signal-absent ground truth.
Its fire rates mix possible occupancy, noise/interference, selection effects, and
algorithm sensitivity. They cannot establish Starlink presence, detection
probability, event count, or a calibrated false-alarm probability. Historical
reproduction also retains the report-era `lnb-a` population exclusion; that is not
a statement that the receiver is currently dead.

### Evidence and exact digests

Frozen leo-tracker reference revision:
`0bb80d14759fd8496b74e7d3219a690be18565a6`.

Evidence root (audit input only, never a runtime path):
`/home/mouse9911/.local/state/leo-flow/evidence/shadow-analysis-v3-backfill-20260817/report-rate-reconstruction/`.

| Evidence | SHA-256 |
|---|---|
| `report-era-thresholds.json` and vendored `report_era_thresholds_v1.json` | `c8f64ab27c1fc2f4aa6a3b55f4bfdb68c72422c9833d6de9728c7f4e54268500` |
| `report-era-fire-rates.json` | `49f1f9af2c8a5aa6c93be93bb3101522078deb4fe4895180b030b9f83c262f2f` |
| `compatibility-matrix.json` | `4214000fd30d84e6a8b5435f8dfc714b0d0f60d467fbc43db6ac3a636dc421df` |
| `bounded-main-v3-fire-evaluation.json` | `00d208814e9a8455709e29a183d4ccc27935fade59d8cd8d52f2d0cedf198644` |
| `full-main-v3-fire-evaluation.json` | `3efc40613aadba090745161a57bfd4accc755560c5e0bac90e52bc17d3a04b5f` |
| reconstruction `README.md` | `b746a77c57f1825065f1e94585e3aeac3090bad8fb3e2f8dc006ca8f81632d07` |
| reconstruction `SHA256SUMS` | `9e20877952833a3d7fef04eba0829cecd97c972ed82a40dec8f836cf5e779b1e` |

The vendored artifact contains all 96 recovered rows for audit completeness. The
Redux compatibility gate exposes only 32 thresholds: eight methods across the four
enabled dimensions above.

### Supersession and rollback

This provisional rule is superseded when exact-cell Redux calibration is accepted
using whole-search null distributions, disjoint holdout validation, and controlled
positive-injection sensitivity gates. It must be disabled or rolled back if any of
the following occurs:

- the vendored artifact digest or pinned source revision cannot be reproduced;
- replay no longer exactly matches the report's per-method fire counts;
- scoring/search semantics or dimension identity cannot be proven exact;
- an enabled cell is found to interpolate, pool, or silently coerce dimensions;
- UI/API output presents a candidate fire as a calibrated beacon detection; or
- independently labeled evidence invalidates the report-era rule's usefulness.

Removal is additive at the consumer/deployment boundary: stop requesting the
provisional contract and retain the immutable artifact and decision record for
reproducibility. Promotion to calibrated detection semantics requires a new,
explicitly approved calibration artifact and contract path; this decision cannot
authorize that promotion.

## 2026-08-17 — Dashboard candidate-score density view

**Status:** accepted for descriptive analysis; not a detection decision surface.

The dashboard exposes a separate aggregate-statistics page backed by the additive
`org.leo-flow` score-distribution v0.1 contract and `/api/v7/score-distributions`.
For every detector-suite method it computes a fixed 40-bin histogram over the
method's native bounded score domain `[0,1]`. Density is `bin_count / total_count /
bin_width`, so every visible method integrates to one and methods with different
sample counts can be overlaid. Raw recording count, score count, mean, population
standard deviation, minimum, and maximum remain visible beside the plot.

The x-axis is never rescaled to an observed per-method minimum and maximum. The
page does not apply the report-era thresholds: current detector-suite v0.2 scores
use an 8 ms prefix and different search multiplicity, whereas the frozen report
thresholds require exact 80/160/640 ms report-era score semantics. The view is
therefore labeled candidate-score density and cannot claim a beacon detection,
detection rate, or calibrated false-alarm probability.

The initial V7 aggregate pooled away scan-section provenance. V8 supersedes it for
the operator page while preserving V7 unchanged. A V8 point has the exact identity
`recording + segment + radio + receiver chain + edge + method`; source-row count
must equal distinct-point count or the query fails closed. Candidate and conditioned
same-section control scores are separate distributions. Radio, RX chain, and edge
filters combine only disjoint server-produced strata. Each point still summarizes
the method's 5–6 supported internal frames; it is not represented as an individual
OFDM-frame score because the v0.2 product does not retain such a value.

## 2026-08-17 — Symmetric rolled-template search control

**Status:** accepted as additive descriptive evidence; historical v0.2 remains
immutable and is not silently reinterpreted.

The v0.2 field named `conditioned_control_score` is not a whole-search null. The
target template first selects the maximum-scoring epoch/CFO hypothesis, then the
rolled template is evaluated only at that target-selected point. Under signal-absent
noise, maximizing the target but not the control creates a winner-selection bias,
so the candidate distribution is expected to exceed this conditioned statistic.

The operator UI therefore labels the existing dashed series **rolled template at
target-selected hypothesis**. The immutable V8 API token remains
`conditioned-control`; changing its meaning or spelling would violate the published
contract.

Redux also adds the separate
`org.leo-flow.starlink-full-search-control-suite/v0.1` evidence path. It maximizes
the rolled template over the same epoch, coarse-CFO, residual-CFO, symbol, and
full-frame selection grid used by the corresponding target method. Relative-phase
methods select their own rolled-template winner. Full-frame ACQUIRE selects the
rolled-template epoch/CFO; VERIFY and FULL are then evaluated at that rolled
ACQUIRE winner, mirroring the target-side split without leaking the target winner.

This symmetric statistic removes the specific target-winner asymmetry, but it is
still a surrogate control, not proof that a scan section is signal-absent. It may
retain sky signal, interference, template autocorrelation, and receiver effects.
The empirical null for calibration must come from independently verified
signal-absent sections and must run the full target search. Existing v0.2 rows show
the new statistic as unavailable until an explicit, versioned back-process creates
the additive product; no historical value may be synthesized from the conditioned
score.

## 2026-08-18 — Periodic historical QAM regression canary

**Status:** accepted as an operational numerical regression; never a detection
calibration.

The retained `2026_08_17_RETRO_QAM` corpus is the immutable acceptance oracle for
the CH4 lower-edge observation at original dwell time 68.7 seconds. Redux must
hash the complete raw CI16 object before each run, recover the v0.3 acquisition
winner independently on RX0 and RX1, reproduce the individual pilot-QAM metrics,
and reproduce the historical inverse-noise dual-receiver improvement within
declared numerical tolerances. `leo-tracker` remains an offline oracle and is not
imported at runtime.

The check runs as an independent oneshot on a 30-minute systemd timer. It has no
radio, capture, network, database, or dashboard capability and cannot delay live
dwells. Each successful run atomically replaces one receipt bound to the corpus,
selected window, algorithm/config identities, source commit, and measured
metrics. A mismatch fails the service. Success means only that the known-pilot
regression agrees with the frozen oracle; the receipt is candidate-only and has
no calibrated detection field or threshold.

The immutable v0.2 acquisition remains unchanged for replay. Acceptance applies
to the additive v0.3 multi-basin acquisition, which covers at least ±400 kHz,
retains separated timing/CFO basins, refines each at sample-level timing and fine
CFO resolution, then selects on held-out pilot symbols. Any future change to the
search space requires a new identity and re-evaluation of its whole-search null
distribution.

## 2026-08-18 — Capture-first continuous analysis and renewable suite leases

**Status:** accepted for the single-station development deployment.

Continuous 60-second CH4-lower capture must never await analysis admission or
completion. Captured pairs remain in the durable SQLite journal and are dispatched
FIFO as bounded analysis capacity becomes available. Analysis processes run at
nice level 15; capture remains at normal priority. The development host permits at
most eight pair analyses at once, while the independent full-dwell workers use the
same lower priority. This preserves CPU preemption for radio capture without
discarding requested QAM, surrogate-null, temporal, waterfall, or Doppler evidence.

A detector-suite job may take longer than its initial 15-minute PostgreSQL lease
for a 60-second recording. Its fenced worker therefore renews the same lease token
and generation every 30 seconds during preparation. Publication occurs only after
the renewal loop stops cleanly; a failed or stale heartbeat prevents publication.
This does not weaken idempotency or permit another worker to publish against an
expired generation. Interrupted processes remain recoverable through normal lease
expiry and exact-scope redispatch.
