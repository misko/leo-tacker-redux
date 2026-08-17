# Decisions

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

