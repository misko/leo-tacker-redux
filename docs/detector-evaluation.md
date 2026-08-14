# Offline detector evaluation reports

`leo_flow.analysis.dataset.evaluate_detectors` is the reporting boundary for
comparing independent-recording detector outputs. It consumes only:

- one already-frozen `DatasetSnapshotBundle`, including its whole-group
  train/validation/locked-test assignments and truth roles;
- the exact `FeatureSetBundle` for every frozen member (canonical bytes must
  match the member's immutable object digest); and
- one versioned `ThresholdRule` calibrated on the training split.

It does not open recording IQ, rerun a detector, fit a threshold, infer a pass
group, or invent a label. Calling `report.canonical_bytes()` produces canonical
JSON and `report.digest` is the SHA-256 content address of exactly those bytes.
Store that payload in the normal content-addressed blob store when an evaluation
becomes a durable artifact; do not create per-window report files on NFS.

## Report interpretation

Each method is identified as `method_id@method_version`. Its threshold, score
semantics, and train/validation/locked-test summaries are frozen into the
report. Every split summary includes:

- FeatureSets with any output for the method;
- union windows, method-present windows, missing windows, and firing count;
- admissible truth, scored prediction, and recording-level true/false
  positive/negative counts; and
- separate missing-prediction, inadmissible-truth, and context-only counts.

A recording predicts target-present when any of that method's windows fires.
No output means a missing prediction, never a non-fire. Accuracy denominators
contain only `SCORED_TRUTH` members for which every evidence item explicitly
declares independence from the exact versioned method. The existing truth
contract always excludes pseudo-labels, TLE/ephemeris-derived associations, and
unlabeled sky. Exact digital injections are admissible only because their
contract pins both the base-noise digest and independent injection-spec digest.

The covariance and phi matrices use binary firings aligned on exact FeatureSet,
segment, receiver, and half-open sample-window coordinates. They use pairwise
complete observations and report both shared-window and shared-sample counts.
Consequently missing method output never becomes zero, phi is `null` for a
constant firing series, and a pairwise-deleted matrix is not assumed to be
positive semidefinite. Overall and per-split matrices are both reported.

## Leakage and scientific claims

The reporter relies on the durable dataset contract to reject a split group
that crosses partitions, and it never reshuffles members. Threshold calibration
is restricted to `train`; its calibration dataset identity and the operator's
train-split assertion are carried into the report. The current threshold-rule
contract does not itself contain a frozen list of calibration members, so the
report emits `calibration-dataset-split-membership-is-operator-attested`. Before
a locked-test claim, archive the training-only calibration snapshot and rule
artifact so that assertion can be independently audited.

`tests/dataset_analysis/test_detector_evaluation.py` is the deterministic,
hardware-free rehearsal. It uses immutable synthetic FeatureSet bundles and
exact injected/independent/proxy truth cases to exercise coverage, missingness,
covariance, phi, label exclusions, split isolation, canonical output, and digest
substitution rejection. The legacy development manifest has no admissible
ground truth and the raw legacy IQ is not present in this repository, so no real
corpus performance run is claimed. An operator must resolve and verify the
manifest's external IQ objects, publish authoritative FeatureSets, construct a
frozen dataset, and then run this reporter. Until independent truth is added,
such a run is exploratory coverage/association analysis, not accuracy evidence.
