# Starlink suite calibration v0.1

Status: additive contract and pure evaluator implemented; no calibration is
approved or deployed.

This layer converts one report-method whole-search score into one calibrated
method decision. It does not change the published v0.2 candidate bundle and it
does not turn eight correlated method decisions into eight beacons. Event
clustering and multi-radio corroboration remain separate post-decision steps.

## Statistical unit

A calibration cell is the exact tuple of:

- method;
- radio and receiver chain;
- hardware-profile and tuning identities;
- channel and edge;
- sample rate and probe sample count;
- algorithm, configuration, exact-template, conditioned-control-template and
  reusable complete-search-profile identities.

The reusable search profile contains the statistical search shape: method,
selection mode, effective cell count, rate, probe length, edge, pilot symbols,
symbol split, conditioning rule, algorithm/configuration and templates. The
v0.2 evidence `search_identity_digest` remains an immutable per-observation
identity and intentionally includes recording/segment provenance. It is
retained in each result but is not used as the reusable calibration key.

Cells are never pooled. Captures below 1.875 MS/s are the clipped-pilot-band
stratum and cannot be calibrated by this contract.

The statistic is the method's `reported_score`, after its complete declared
search has selected the maximum. The report-compatible comparison is strictly
`score > threshold`; a score equal to the threshold does not fire. Roll-17 is
still conditioned at the selected exact cell and is diagnostic evidence, not
the threshold statistic.

## Promotion gates

For each exact cell:

1. Freeze disjoint training-null, holdout-null and positive-injection members
   before fitting.
2. Fit the threshold only from training whole-search maxima.
3. Audit the threshold once on holdout whole-search maxima. The one-sided
   Wilson upper confidence bound on FAR must not exceed the declared target.
4. At every declared injected-SNR point, the one-sided Wilson lower confidence
   bound on detection probability must meet its declared minimum.
5. Approve the threshold only if every null and positive gate passes.
6. Apply it only when every statistical-cell identity exactly matches the
   candidate evidence.

The default one-percent whole-search FAR plan requires 10,000 training-null
maxima, giving 100 expected training-tail observations, and 4,000 disjoint
holdout-null maxima. The latter is the minimum plan whose one-sided 95% Wilson
upper bound passes at the declared half-target design FAR with at least 20
expected exceedances. These counts apply independently to every exact cell;
they are not a license to pool radios, receivers, tunings, rates or methods.

An approved result is a method decision with explicit calibration evidence.
It carries `method-decision-not-beacon-count` and
`event-clustering-required`; downstream code must not sum correlated report
methods as independent detections.

## Current operational state

The implementation and component tests establish these semantics, including
strict comparison, identity mismatch rejection, clipped-band separation,
held-out FAR confidence and positive-injection confidence. No production
threshold has been promoted. The existing live dashboard therefore correctly
shows candidate scores with a null calibrated-detection count.

Promotion still requires a frozen independent corpus for every deployed cell,
an integration-owned durable pipeline and dashboard contract, and a controlled
end-to-end calibration run. `leo-tracker` may generate numerical oracle
evidence for those tests but is never imported by the runtime.
