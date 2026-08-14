# Offline residual tracking experiment

The prototype in `leo_flow.analysis.tracking` asks one bounded question: after
independent recordings have all selected the same NORAD hypothesis, can a
two-state model describe their RF residuals without hiding discontinuities?

| Item | Meaning |
|---|---|
| Identity | One fixed NORAD ID for the complete run |
| State 0 | Measured minus orbit/RF-predicted carrier frequency, Hz |
| State 1 | Measured minus orbit/RF-predicted frequency drift, Hz/s |
| Ordering | Caller-provided, strictly increasing UTC nanoseconds |
| Context | Exact receiver chain, hardware snapshot, station and geometry digest |
| Gate | Reported normalized innovation squared against a pinned threshold |
| Output | Explicit segments, rejected observations, filtered and optional smoothed covariance |

The input must include the exact association request matching the decision
digest. Ambiguous/no-match decisions, propagation failures, a different NORAD
ID, and innovation outliers are never converted into measurements. Context
changes and large gaps either segment or reject according to explicit policy.

The seeded fixture under `tests/model_analysis/fixtures` is independently
injected software truth. It checks determinism and catches numerical or wiring
regressions; it is not real-world ground truth and sets no production accuracy
claim. This module should remain offline until residual distributions and
uncertainties are evaluated on calibrated recordings.
