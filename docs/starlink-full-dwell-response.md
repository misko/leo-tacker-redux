# Full-dwell Starlink detector response v0.1

This additive product answers a narrower question than the V13 temporal-pilot
trace: where, across an entire dwell, should the complete detector suite be
run, and what did every method return in each exact selected window? It does
not alter the V13 contracts or imply a calibrated detection.

## Frozen semantics

The product has two stages per recording, segment, radio, receiver, and edge.

1. The exhaustive prescreen partitions (or overlaps across) the complete
   segment with endpoint-preserving windows. It records mean complex power,
   exact sample/UTC intervals, and whether each window was selected. Its
   interval union must cover 100% of the segment. The prescreen is
   pattern-blind and is performed independently for every receiver stream.
2. Exact refinement selects the highest-power prescreen windows, breaking ties
   by earliest sample. The same selected windows and frozen search grid are
   applied to Qin and every precommitted surrogate. Each selected window emits
   all eight methods in `REPORT_METHOD_ORDER`.

Each exact point identifies one recording/segment/radio/RX/edge/method/window.
Its score is the maximum over that method's declared epoch/CFO cells, not a
mean. The point stores the winning window-relative and segment-relative epoch,
coarse and residual CFO, effective search-cell count, Qin score, every
surrogate score and template digest, finite upper-tail rank, Qin-minus-largest-
surrogate margin, exact half-open sample/UTC interval, prescreen score, and
dependence group.

The bundle reports prescreen coverage and exact-detector coverage separately.
Selected exact windows are never described as exhaustive exact coverage.
Overlapping windows and power-selected refinements are statistically dependent;
finite surrogate rank is not a p-value; all time/search look-elsewhere effects
remain uncalibrated.

## Why exact 8 ms coverage is not the default

A read-only benchmark used the retained RETRO QAM recording:

`/mnt/qnap01/mouse9911/leo-store/2026_08_17_RETRO_QAM/raw/clip-002.ci16`

The clip is 500,200,000 bytes, 62,525,000 dual-RX samples at 2.5 MS/s, or
25.01 seconds. On the benchmark host, exhaustive non-overlapping 20,000-sample
power prescreening covered all 3,127 windows in 0.108 seconds (579.4
MSamples/s). One 20,000-sample exact window using the current production grid,
all eight methods, Qin, and four surrogates took 15.788 seconds.

Consequently, exact 8 ms analysis of all 3,127 windows is linearly estimated at
49,370 seconds (13.7 hours) per RX. Thirteen 2-second exact coarse windows have
similar total detector work and a 51,312-second linear estimate. These are
operation-count extrapolations, not claimed large-window wall-clock
measurements.

The frozen practical plan for a normal 20-second, 2.5-MS/s dwell is:

| Parameter | Value |
|---|---:|
| Prescreen window / stride | 20,000 / 20,000 samples (8 ms) |
| Prescreen coverage | 2,500 windows, 100% interval union |
| Exact window | 20,000 samples |
| Exact selection | top 32 power windows, ties by start |
| Exact detector coverage | at most 640,000 samples / 1.28% before overlap |
| Patterns | Qin + four fixed precommitted surrogates |
| Methods | all eight, frozen order |
| Estimated exact time | about 505 seconds per RX on the benchmark host |

This selection is computationally bounded and identical for Qin/surrogates,
but it can miss a low-power structured signal. That limitation is explicit in
the contract and should be evaluated before changing the frozen selector.

## Persistence and dashboard integration inventory

The current change owns only contracts, native analysis, codec, tests, and the
read-only benchmark. The integration steward should add the following without
changing existing product contracts:

- an object-store writer/reader using format ID
  `starlink-full-dwell-response-v0.1` and the 256 MiB codec bound;
- a catalog projection keyed by analysis ID, recording ID, immutable recording
  digest, request digest, source-suite reference, stream count, prescreen count,
  exact-window count, and point count;
- idempotent submission/work orchestration and a migration owned by the
  integration component;
- a narrow query DTO that filters method, segment, radio, RX, and edge and
  decimates only for transport while retaining first/last and extrema;
- an additive route such as
  `/api/v15/recordings/{recording_id}/starlink-full-dwell-response`; the final
  version number belongs to dashboard integration;
- a recording-detail plot with the complete power prescreen as background and
  exact Qin/surrogate points as overlays. Tooltips must expose exact coverage,
  selection status, winner coordinates, rank/margin, and dependence warnings;
- UI and API tests proving radio/RX streams are never pooled and that transport
  decimation cannot be confused with scientific coverage.

No schema migration, deployment wiring, live-state write, or dashboard route is
included here.
