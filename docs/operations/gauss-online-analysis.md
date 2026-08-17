# Campaign-scoped online analysis

`drain-analysis-online` processes only complete 36-batch windows already
published by an open 936-batch main campaign. It runs FeatureSet, waterfall,
and Starlink-suite compute/projection stages but does not modify the capture
journal, close RF collection, reconcile campaign receipts, or access a radio.

The command owns `--online-analysis-lock`, not `--campaign-lock` or the radio
mode lock. SQLite WAL reads therefore overlap the capture writer without
weakening capture CAS. A partial 1–35 batch tail and any planned/in-flight
record are excluded from its public input view.

Before workers start, the preparer validates the immutable terminal snapshots,
checks both successful recording identities and CAS digests, submits exact
deterministic jobs, and registers the resulting window through
`register_campaign_analysis_window_scope_v1`. PostgreSQL rejects registration
unless all 216 jobs have the expected type and recording ID.

Release G capture preflight calls
`capture_registered_analysis_safe_v2(definition_digest)`. Active leases are
accepted only when their source job belongs to an exact terminal window
registered through the 0032 scope port. The registered window may belong to an
older campaign, allowing bounded historical CPU-only backfill without pausing
RF capture. An unregistered job, legacy Starlink work, model work, or any
database uncertainty closes admission. Release F retains the stricter
same-campaign v1 gate. Existing lease tokens, generations, expiry fencing,
exact-ID claims, and stage barriers remain unchanged.

Feature projection preserves parallelism without avoidable retry conflicts:
workers receive deterministic batch-affine shards, and both recordings from a
capture batch always project through the same worker. Different batches still
project concurrently. Waterfall and Starlink-suite projection retain the full
configured worker count because they do not share the batch-row write point.

The online pass stops at durable projection completion. After RF closure,
`drain-analysis-staged` revisits the same deterministic jobs, observes their
terminal state, serially reconciles all 936 immutable campaign receipts, and
closes the dashboard/campaign counts exactly. Thus online work changes latency,
not campaign evidence or replay policy.

The user timer runs one bounded slice per minute. Exit 75 means the bounded
slice has retryable work and is considered a successful timer invocation;
parked work exits 4 and requires operator inspection. The timer must only be
enabled from a sealed release containing migration 0033.
