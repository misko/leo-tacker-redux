# Dataset construction and truth plan

## Flow

| Stage | Input | Output | Forbidden dependency |
|---|---|---|---|
| Independent analysis | one immutable recording | one immutable FeatureSet | dataset partitions, TLE/model state |
| Dataset construction | FeatureSet refs, capture grouping, independent truth | frozen split membership and diagnostics | IQ access, detector execution, fitted model |
| Cross-recording fit | one frozen dataset plus pinned hardware/TLE snapshots | model snapshot | mutation of membership or labels |
| Evaluation | frozen model/config and sealed partition | metrics with uncertainty | threshold/model refit on evaluated partition |

## Required split unit

Use the coarsest credible dependency boundary. A group contains all recordings
from one satellite pass or unknown-pass time neighborhood within a contiguous
station session, including simultaneous radios, both LNBs, derived windows, and
every injection using the same noise. Allocate whole groups in time order. For
claims across hardware, additionally hold out a full radio/LNB/firmware epoch.

The API requires a reviewed `group_partitions` mapping. It deliberately has no
seed, percentage, or random-shuffle option. The stored member tuple pins the
FeatureSet ID, its content digest, partition, and scored/context-only role;
canonical hashing freezes it.

## What counts as truth

| Source | Accuracy truth? | Required provenance |
|---|---:|---|
| Controlled observed instrument/RF | Yes | instrument/settings digest, uncertainty, independence declaration |
| Manual | Conditional | reviewed evidence digest and independence from evaluated method |
| Digital injection | Yes | base recording hash, independent injection spec/hash, exact parameters |
| Ephemeris-derived | No by itself | TLE snapshot and association policy; use as model input |
| Pseudo-label | No | source model/config/dataset digests; exploration only |
| Unlabeled | No | never reinterpret absence of a label as a negative |

No label is valid for a tested method if that method produced the selection or
evidence. Hard nulls must be selected independently of candidate scores.

## Diagnostics and method covariance

Every snapshot reports counts by partition and distributions by radio, LNB,
mode, sample rate, gain, satellite, UTC day, and truth source. A release review
should additionally inspect intersections, not just marginal balance.

Method firing association uses exact common sample windows. For every method
pair it reports binary firing covariance, phi when both variances are nonzero,
shared window count, shared sample count, and per-method missing windows.
Results with different sample counts must not be visually compared as though
they arose from a single complete matrix.

## Promotion gates and current gaps

Promotion of the scored subset requires at least three independent groups; nonempty train,
validation, and sealed test partitions; independent positive and negative
labels; exact injections; time-ordered partitions; and no pseudo,
ephemeris-only, or unlabeled members in the scored truth set. Such members may
remain explicitly context-only. The existing Wave 0 corpus fails the scored
truth gates by design.

Data still needed:

- several disjoint days and complete, independently assigned pass groups;
- digital injections across real-noise backgrounds spanning SNR, drift,
  frequency offset, delay, gain imbalance, clipping, and V5 continuity gaps;
- controlled RF positives plus score-blind hard nulls and interference labels;
- temperature and LNB swap epochs and at least one completely held-out radio;
- enough independent null groups for a false-alarm confidence interval;
- a sealed manifest whose labels are withheld from implementers until the
  algorithm/config/dataset digests and pass/fail criteria are frozen.
