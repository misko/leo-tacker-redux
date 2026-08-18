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

## Persistence, dashboard, and resource policy

The additive V15 integration stores the canonical bundle CAS-first under format
ID `starlink-full-dwell-response-v0.1`, then atomically publishes its immutable
catalog identity and normalized window points through migration 0041. Exact
replay succeeds; any idempotency, scientific-identity, object-metadata, or byte
conflict fails closed. The live-reference view and insert trigger prevent GC
from retiring a published bundle. Dashboard access is through one bounded
`SECURITY DEFINER` read routine and never exposes a locator.

`/api/v15/recordings/{recording_id}/starlink-full-dwell` filters methods,
radios, receiver chains, and edges independently, with a hard 4,096-point
transport cap. The `/full-dwell` page plots Qin and the precommitted surrogates,
reports queue/backlog/truncation states, and keeps radio and RX filters separate.
V13 and every earlier product remain immutable.

Full-dwell work is an optional asynchronous analysis lane. It must use a bounded
queue and bounded worker concurrency; queue admission or saturation must never
block, fail, or delay capture. A rejected admission is recorded explicitly as
backlog/truncation rather than silently dropping selected windows. With the
approved top-32 plan, measured implications are about 505 seconds per RX; a
two-RX dwell therefore consumes about 1,010 worker-seconds before contention.
Operators must size concurrency against this service time, publish backlog
depth, and preserve pending/error terminal state. No synchronous call from the
continuous capture loop is permitted.

The dashboard language is deliberately asymmetric: “100% pattern-blind power
prescreen coverage” and “sparse selected exact detector coverage (typically
about 1.28%).” Sparse exact points must never be called full detector coverage.
Transport truncation is separate again and does not change either scientific
coverage measure.
