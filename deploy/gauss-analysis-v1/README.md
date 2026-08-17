# Gauss post-capture analysis v1

This checked development deployment analyzes only durable jobs that an operator
submits after capture publication. It does not contact a radio, inspect capture
state, scan the CAS, or infer work from filenames. The catalog resolves one exact
`recording_id`; the queue payload pins the complete published recording object,
algorithm, configuration, dependency, and output schema references.

First validate the exact local service and scientific approval without opening
the credential, catalog, or CAS:

```console
python -m leo_station.analysis_operator validate \
  --config deploy/gauss-analysis-v1/analysis.json \
  --science-manifest deploy/gauss-analysis-v1/science.json
```

After a capture has been published, submit only its returned `recording_id`:

```console
python -m leo_station.analysis_operator submit \
  --recording-id rec_REPLACE_WITH_PUBLISHED_ID \
  --science-manifest deploy/gauss-analysis-v1/science.json \
  --credential-directory /run/credentials/gauss-analysis
```

For a dual capture, supply the exact canonical public `CaptureBatchSnapshot`
document emitted by the dual operator. The analysis operator never reads the
capture SQLite database, enumerates the CAS, or constructs a recording path:

```console
python -m leo_station.analysis_operator submit-batch \
  --batch-snapshot batch-terminal.snapshot.json \
  --science-manifest deploy/gauss-analysis-v1/science.json \
  --credential-directory /run/credentials/gauss-analysis
```

Both attempts must be terminal. Every successful `PublishedRecordingRef` is
compared exactly with the public catalog before any job is enqueued. A success
is retained when its peer failed. `paired_analysis_eligibility` is reported as
a batch fact, while `paired_science_submitted` remains `false` because this
approval contains only the pinned per-recording analyzer.

Before catalog verification and enqueue, the station composition republishes
the deterministic initial public batch view. Exact replay is a no-op, while a
projection failure prevents job enqueue. This lets analysis repair a prior
capture-to-dashboard publication outage without inspecting capture-private
SQLite state.

Then explicitly run at most one local analysis unit:

```console
python -m leo_station.analysis_operator process-one \
  --config deploy/gauss-analysis-v1/analysis.json \
  --science-manifest deploy/gauss-analysis-v1/science.json \
  --credential-directory /run/credentials/gauss-analysis
```

Successful recording analysis atomically publishes both the FeatureSet and its
dedicated durable projection work. Drive at most one such exact public result
through the idempotent dashboard projection worker with:

```console
python -m leo_station.analysis_operator project-one \
  --science-manifest deploy/gauss-analysis-v1/science.json \
  --credential-directory /run/credentials/gauss-analysis
```

Every command emits JSON Lines. `submit` is idempotent for the same exact public
recording and science selection. `process-one` can be repeated; durable leasing,
generation fencing, and atomic FeatureSet publication make restart behavior
explicit. Feature projection and dashboard serving remain independent downstream
processes. `project-one` resolves only the exact `FeatureSetRef` and published
`RecordingObjectRef` carried by its leased outbox item; it does not scan the CAS.

The authoritative Starlink candidate lane is separate from FeatureSet and
waterfall work. It admits complete 2.5 and 5 MS/s edge-scan recordings whose
segment tags say the full pilot band fits. Each rate has a distinct pinned
config, template identity, epoch grid, and 8 ms probe bound. Selection covers
every segment and receiver, derives lower/upper identity from the immutable
segment tags, and pins the frozen Qin Appendix-A exact and roll-17 control
templates. The 1.875 MHz pilot allocation leaves 312.5 kHz total sampled guard
at 2.5 MS/s and 3.125 MHz at 5 MS/s; a 1.25 MS/s capture clips 625 kHz (one
third) and is deliberately refused. Both approved profiles search 53 epoch
hypotheses by 11 CFO hypotheses (583 cells); epoch strides are 64 and 128
samples respectively, the same 25.6 microseconds. The checked approval has
canonical digest
`sha256:519834edfb2724599d4ceb1fade922e1a9e7a82f8acf22f310a30f12ee84e0b6`;
the exact `science.json` file SHA-256 is
`f8d000718546866579c58f83b975c39f6ba0e970be613ad847f5bbfba9d56094`.
Run these three bounded commands once per published recording,
repeating the last two until `forward_progress` is `false`:

```console
python -m leo_station.analysis_operator submit-starlink \
  --recording-id rec_REPLACE_WITH_PUBLISHED_ID \
  --science-manifest deploy/gauss-analysis-v1/science.json \
  --credential-directory /run/credentials/gauss-analysis
python -m leo_station.analysis_operator process-starlink-one \
  --science-manifest deploy/gauss-analysis-v1/science.json \
  --credential-directory /run/credentials/gauss-analysis
python -m leo_station.analysis_operator project-starlink-one \
  --science-manifest deploy/gauss-analysis-v1/science.json \
  --credential-directory /run/credentials/gauss-analysis
```

These commands publish search candidates, scores, control scores, margins, and
search identity—not detections. The dashboard must continue to display
`calibration required` and no detection count until an exact matching approved
whole-search calibration artifact is added through a later versioned path.

The complete post-capture path can instead be run as one explicitly bounded
operation. It holds the shared Gauss mode lock while submitting the terminal
batch, processing jobs, and draining currently claimable feature projection
work until each worker reports no claimable item or its bound is reached:

```console
python -m leo_station.analysis_operator drain-batch \
  --batch-snapshot batch-terminal.snapshot.json \
  --config deploy/gauss-analysis-v1/analysis.json \
  --science-manifest deploy/gauss-analysis-v1/science.json \
  --credential-directory /run/credentials/gauss-analysis \
  --max-analysis-jobs 4 \
  --max-projection-work 4
```

This is a bounded operator action, not a daemon or shell workflow. The
`analysis_no_claimable_work` and `feature_projection_no_claimable_work` fields
describe only the final immediate claim attempt. They do not prove that delayed
retries, parked work, or other future work is absent. `false` means the bound
was reached and the operator must review the queue before another invocation.

The CAS root is exactly
`/home/mouse9911/.local/share/leo-flow/objects`, shared by public object
reference rather than constructed private paths. Capture and analysis use the
same nonblocking mode lock at
`/home/mouse9911/.local/state/leo-flow/pipeline-mode.lock`; `process-one`,
`project-one`, all submission commands, and `drain-batch` fail before
credential, database, or CAS use if capture owns it. The checked dependency
refs pin the Python 3.11.16 Gauss analysis
environment and the exact `uv.lock` digest.
Python 3.14 remains useful for control-plane testing but is not approved for this
science path because its numeric ephemeris receipts differ. Promotion requires
replacing this development approval with measured, committed release evidence.
