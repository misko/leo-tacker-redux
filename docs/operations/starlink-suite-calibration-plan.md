# Starlink suite calibration execution plan

Status: source planner and stable-profile derivation are implemented. No live
calibration threshold is approved or deployed.

## Current deployed inventory

A read-only audit of the first seven completed V5 windows (`0..251`) covered
504 recording-suite products: 336 eligible candidate recordings and 168
clipped `not_evaluated` recordings. The eligible products contain 43,008
method observations.

Those observations have 43,008 distinct immutable v0.2 observation-search
identities because each closes over its recording and segment. They reduce to
32 reusable statistical search profiles and 512 exact deployed calibration
cells when combined with radio, receiver, channel, edge, rate/probe and method:

- 2 radios;
- 2 receiver chains per radio;
- 4 channels;
- 2 pilot edges;
- 2 eligible rate/probe profiles: 2.5 MS/s × 20,000 samples and 5 MS/s ×
  40,000 samples (both 8 ms);
- 8 report methods.

That is 32 cells per rate/method and 512 cells total. No reusable-profile drift
was observed over the seven windows. The operative Release-F station files are
bound by SHA-256:

- R20: `5ed22706afba54e56bfb10d94652fddc2bf2ef5b9094d59d984c7c4b5698d643`
- R21: `61cd7fa2c904140aa34e4656e589763e73283d7c6489dc2772089082b0a0ef77`

Any hardware-profile or tuning-epoch change creates new cells; it is never
silently pooled into these 512.

## Required corpus at the default gate

At a one-percent whole-search FAR target, each exact cell requires:

- 10,000 frozen training-null whole-search maxima;
- 4,000 disjoint holdout-null whole-search maxima;
- the declared positive-injection trials at every SNR/CFO/epoch/occupancy/
  drift gate.

For the current 512 cells, the null workload is therefore 5,120,000 training
maxima plus 2,048,000 holdout maxima, or 7,168,000 complete-search null trials.
Positive trials are additional and must be fixed before corpus generation.

## Execution sequence

1. Freeze the hardware/tuning epoch and materialize all 512 exact cell plans.
2. Freeze mutually disjoint training-null, holdout-null and positive-injection
   member manifests. Independently searched cross-edge target-code-free trials
   are valid null inputs; conditioned roll-17 scores are not null trials.
3. Run the complete declared search for every corpus member. Store one maximum
   `reported_score` per method/cell/trial, never per-point samples.
4. Fit each threshold only on its training nulls using the strict report rule
   `reported_score > threshold`.
5. Evaluate the frozen threshold once on its holdout nulls. Reject the cell if
   the one-sided Wilson FAR upper bound exceeds the target.
6. Reject the cell unless every positive-injection SNR gate meets its declared
   one-sided Wilson detection-probability lower bound.
7. Publish approved cell calibrations and evaluate candidate methods only on
   exact identity matches. An unapproved or missing cell stays uncalibrated.
8. Cluster calibrated method decisions into de-duplicated events. Correlated
   report methods are comparison evidence and are never summed as beacons.
9. Project calibration identity, threshold, method decisions and clustered
   event counts into a new additive dashboard contract. Preserve the v0.2
   candidate API unchanged.

## Promotion gates

Production promotion requires all 512 current cells (or an explicitly smaller
declared deployment subset) to have accepted null and positive evidence,
durable exact-replay persistence, integration-owned PostgreSQL and dashboard
tests, a sealed release, and an end-to-end controlled run. Until then, the
dashboard's null calibrated-detection count is the only truthful state.
