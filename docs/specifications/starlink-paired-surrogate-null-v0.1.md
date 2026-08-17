# Starlink paired surrogate controls v0.1

## Decision and terminology

Before this slice, Redux did **not** have a common Starlink detector interface.
`StarlinkDetectorSuiteV0_2.analyze_receiver` ran the eight report methods for
the Qin template, while `analyze_full_search_control` independently searched
one roll-17 template. The v0.2 evidence also retains a same-cell roll-17 score,
which is useful as a diagnostic but is not a whole-search null.

This additive slice introduces the structural port
`StarlinkDetectorV0_1.detect(radio_signal, parameters)`. It is a `Protocol`, not
a base class: detector implementations are composed behind the port rather
than coupled by inheritance. `ReportMethodStarlinkDetectorV0_1` adapts the
existing v0.2 numerical implementation without changing its published
contracts. The exact Qin pattern and every surrogate pass through this one
method and the same eight-method search implementation.

The persisted term is **paired surrogate control**, not “known null.” These
patterns are not verified signal-absent recordings, and their finite score
sample is not a calibrated detection distribution.

## Precommitted codebook

The v0.1 surrogate codebook is fixed before samples are read:

- generator: `splitmix64-qpsk-edge-codebook-v0.1`;
- master seed: `0xD1B54A32D192ED03`;
- codebook index: `0..31`;
- each index expands to a 300-by-8 matrix over states `{0,1,2,3}`;
- the state maps to the same QPSK phase convention as the Qin Appendix-A
  template;
- the edge's same eight pilot subcarriers, 750 Hz frame cadence, pilot symbol
  interval 2..301, sample dimensions, and template energy are preserved;
- synthesis is deterministic for the declared edge and sample rate;
- seed and state-matrix digest never depend on IQ samples, recording identity,
  observed score, time, radio, receiver, or host.

Every pattern records its role, template reference and digest, generator,
seed, codebook index, state-matrix digest, edge bins, dimensions, and energy.
The exact Qin pattern has explicit `null` seed/index fields because it is not a
surrogate.

The normal invocation uses four distinct surrogates. Callers may explicitly
request 1..32. Four gives only coarse empirical rank resolution (increments of
`1/(4+1)`) and must not be presented as a well-resolved tail probability.
Increasing the count improves the finite paired comparison but still does not
create calibrated false-alarm probabilities.

## Identical search semantics

Each invocation produces all report methods in frozen order:

1. Anchor-8
2. Differential-16
3. Differential-32
4. GLRT-32
5. GLRT-64
6. Full-frame acquire
7. Full-frame verify
8. Full-frame full

For a given method, target and surrogates share one `search_plan_digest`, which
covers the algorithm/config references, epoch and CFO grid identity, method,
effective cell count, pilot symbols, split, selection rule, and independent
per-pattern maximization. Each pattern has a different `search_identity_digest`
because that digest additionally covers the input and pattern reference.

Every pattern repeats the entire relevant selection/search. In particular,
full-frame acquire independently selects a winner for that pattern. Its verify
and full statistics are then evaluated only at **that same pattern's** acquire
winner. A surrogate is never evaluated at Qin's winner, nor at another
surrogate's winner.

## Evidence and interpretation

`StarlinkPairedSurrogateEvidenceV0_1` is persist-ready canonical JSON evidence.
For every pattern and method it retains:

- score and frame summary;
- effective search-cell count and search-plan identity;
- winning epoch, coarse CFO, and residual CFO;
- search/selection mode and pilot symbol set;
- pattern reference, seed, index, and state/template digests;
- immutable input identity and digest;
- algorithm/config references and provenance.

The per-method finite upper-tail rank is
`(1 + count(surrogate_score >= Qin_score)) / (surrogate_count + 1)`. It is
reported only as a paired-surrogate descriptive statistic. Evidence is always
candidate-only and carries the disclosures:
`finite-paired-surrogate-controls`, `not-verified-signal-absent`, and
`not-calibrated-detection`.

## Durable component boundary

The additive durable layer uses
`StarlinkSurrogateNullRequestV0_1` and
`StarlinkSurrogateNullRecordingBundleV0_1`. A request pins:

- the immutable recording pair;
- the source v0.2 suite artifact and source-request digest;
- the complete epoch/coarse-CFO/residual-CFO grid and resource bounds;
- exact source-suite stream membership and probe lengths;
- the requested surrogate count and output schema.

`StarlinkSurrogateNullAnalysisPreparerV0_1.prepare_after_suite` accepts an
existing v0.2 request/bundle pair, checks recording, configuration and stream
membership, derives the new request, and reopens the recording through the
narrow `RecordingObjectReader` port. It does not modify, wrap, or reinterpret
the source v0.2 bundle. `ExactStarlinkSurrogateNullRecordingAnalyzerV0_1`
records radio, channel, edge and exact probe time bounds alongside each
stream's paired evidence.

`DurableStarlinkSurrogateNullStoreV0_1` writes canonical bytes through a blob
port and publishes only an immutable catalog projection. Exact repeats of an
idempotency key must resolve to the same projection and object reference;
conflicting reuse raises `StarlinkSurrogateNullConflictError`. Reads verify
catalog identity, object metadata, verified state, byte length, SHA-256,
canonical decoding, nested evidence closure, and a reconstructed projection.
Concrete catalog/SQL behavior remains integration-owned.

## Dashboard/query handoff

The narrow query port is
`RecordingStarlinkSurrogateNullQueryPortV0_1.recording_starlink_surrogate_null(query)`.
`StarlinkSurrogateNullQueryV0_1` supports bounded filters for recording,
method, radio, channel, edge, and overlapping UTC interval. At most 512 detail
rows are returned; aggregate values cover all matching rows before truncation.

The exact dashboard field mapping is:

| UI meaning | DTO field |
|---|---|
| method | `rows[].method` |
| radio / receiver | `rows[].radio_id`, `rows[].receiver_chain_id` |
| channel / edge | `rows[].channel_number`, `rows[].edge` |
| analyzed time | `rows[].interval_start_utc_ns`, `rows[].interval_stop_utc_ns` |
| Qin score | `rows[].qin_score` |
| every surrogate score | `rows[].surrogate_scores[]` |
| surrogate identity/seed/index | `rows[].surrogate_patterns[]` |
| descriptive rank | `rows[].finite_upper_tail_rank` |
| Qin search winner | `rows[].qin_winning_epoch_sample`, `rows[].qin_winning_coarse_cfo_hz`, `rows[].qin_winning_residual_cfo_hz` |
| scientific provenance | `rows[].provenance` |
| calibrated p-value | `rows[].calibrated_p_value` (always `null`) |
| calibrated detection | `rows[].calibrated_detection` (always `null`) |

Per-method aggregates expose row count, mean Qin score, mean surrogate score,
mean finite upper-tail rank, and count of Qin scores above every paired
surrogate. The statistic label is fixed to
`finite-paired-upper-tail-rank-not-calibrated-p-value`; the view also carries
`candidate-evidence-not-detection`. A UI must preserve those labels and must
not abbreviate the finite rank as a p-value.

## Runtime integration handoff

This slice intentionally does not change jobs, PostgreSQL, dashboard,
deployment, or live capture. The integration steward can wire it by:

1. constructing `StarlinkRadioSignalV0_1` from the already bounded immutable
   receiver probe;
2. reusing the deployed `StarlinkDetectorSuiteConfigV0_2`;
3. constructing `ReportMethodStarlinkDetectorV0_1` from the analysis execution
   context and wrapping it in `StarlinkPairedSurrogateAnalyzerV0_1`;
4. encoding the result with `encode_paired_surrogate_evidence` and publishing
   it through a new additive artifact/receipt contract;
5. exposing per-method Qin and surrogate score samples as candidate evidence,
   never converting their finite ranks into detection counts without a
   separately approved whole-search calibration.

`leo-tracker` remains a numerical oracle only and is not imported at runtime.
