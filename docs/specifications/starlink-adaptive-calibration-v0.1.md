# Adaptive Starlink/QAM calibration v0.1

Status: additive offline contract and pure evaluator implemented. No threshold
is approved, deployed, or available for a dashboard decision.

## Frozen experiment boundary

The independent unit is one complete dwell in one exact calibration cell. A
plan precommits three immutable manifests: train, validation, and locked test.
Both member and acquisition-group digests must be unique and disjoint, so
windows or derived variants from one recording cannot cross splits. Only
train-null labels may fit the threshold. Validation is evaluated without
refitting; the locked test may be opened only after validation passes.

Every dwell contains Qin at pattern index zero and every precommitted surrogate
at the remaining indices, with the same radio/RX inventory for every pattern.
The supplied receiver score must already be the maximum across the complete
time, CFO, epoch, and any other adaptive search grid. The evaluator then takes
the maximum across receivers and patterns. This one family-wise maximum per
null dwell is the calibration statistic. A search that produces no candidate
is retained with candidate count zero and maximum zero; it is never dropped or
conditioned away.

The threshold is deterministic. For `n` train-null dwells and target
family-wise FAR `alpha`, it is descending order statistic
`floor(n * alpha)`, with a strict `score > threshold` decision. If
`floor(n * alpha)` is zero, fitting fails because the requested tail cannot be
resolved. The contract reports the rank, count, and minimum finite-sample
resolution `1/n`.

## Held-out gates

Null evidence tests the family-wise Qin-or-surrogate decision. Positive
evidence tests the precommitted Qin target only. Each may require a temporal
response gate and explicit coherent QAM evidence from at least two receivers;
each counted coherent receiver must independently pass both gates. The same
fields and rules exist for Qin and every surrogate under the null.
Receiver labels and radio/RX/pattern enumeration cannot affect a maximum.

Validation and locked test report raw false-alarm/detection counts and one-sided
Wilson FAR upper and detection-probability lower confidence bounds. They never
report an unsupported p-value. A result remains candidate-only unless both
held-out splits pass their plan-declared target FAR and minimum detection
probability. Opening the locked test once remains an operator/governance
requirement; this pure offline evaluator has no mutable registry.

## Scientific blockers to promotion

- There is not yet a sufficiently large, frozen, independent corpus of null
  dwells and positive dwells for each exact hardware/search cell.
- The RETRO QAM fixture is one conditioned historical positive. It verifies
  dual-RX plumbing and guards a known signal, but it is not an independent
  estimate of detection probability and synthetic repetition does not add
  sample size.
- Upstream producers must prove that scores are complete-search maxima and that
  Qin and surrogate patterns use identical windows, CFO/epoch grids,
  preprocessing, and candidate handling. This consumer cannot reconstruct
  omitted search cells from a scalar maximum.
- A precommitted definition and blinded implementation of dual-RX temporal/QAM
  coherence are required. A high QAM fit selected after inspecting the target
  cannot be used as confirmatory evidence.
- Target FAR, minimum detection probability, confidence, calibration cells,
  surrogate bank, and all evidence gates must be chosen before freezing the
  manifests. Exploratory choices remain candidate-only.
