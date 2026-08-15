# Durable offline Starlink-like E2E harness

`benchmark.starlink_durable_e2e` is a bounded, deterministic, hardware-free
composition test of the public pipeline:

```text
synthetic paired CI16 + capture plan
  -> RecordingManifest + SQLite local spool + SigMF pair
  -> filesystem content-addressed handoff and local cleanup
  -> IndependentDetectorSuite + durable FeatureSet
  -> frozen dataset + TRAIN-only thresholds
  -> aggregate detector evaluation + canonical JSON report
```

It imports neither a physical-radio driver nor a live database/service adapter.
It does not contact `.15`, PostgreSQL, shared storage, or any network service.

Run it from the repository root with fresh paths outside Git:

```text
.venv/bin/python -m benchmark.starlink_durable_e2e \
  --workspace /tmp/starlink-durable-e2e-workspace \
  --output /tmp/starlink-durable-e2e-report.json
```

The workspace must be absent or empty. It intentionally remains after the run
so another `FileSystemBlobStore` instance can verify the content-addressed
recordings, truth, FeatureSets, dataset, evaluation, and report. The requested
output is byte-identical to the report in CAS. Do not select a repository path
for either argument.

## Frozen bounds and split isolation

The v1 harness has six cases: one null and one +6 dB coded-edge-pilot injection
on each of three independently seeded backgrounds. Whole background groups are
preassigned to TRAIN, validation, and locked test. Only the two TRAIN members
reach threshold calibration; the frozen rule is then applied unchanged to all
partitions.

The harness fails closed above any of these limits:

| Resource | Bound |
|---|---:|
| Cases | 6 |
| Detector windows | 48 |
| Generated paired IQ | 1,572,864 bytes |
| Unique durable artifact payloads before the final report | 16 MiB |
| Machine-readable report | 1 MiB |
| Runtime | 120 seconds |

No IQ or per-window artifact belongs in Git. Each successful recording is
first finalized through the capture writer and durable SQLite spool, published
as exact data/metadata objects into local CAS, reopened through the normal
recording reader, and removed from the capture-local recording directory only
after acknowledgment.

## Failure arms

Two predeclared arms prove failure behavior through the same capture engine and
writer:

- `truncation` supplies 4,095 of 4,096 requested paired frames and must raise
  `SampleCountError`;
- `missing_frame` supplies the full stored byte count but advances the second
  refill's sample sequence by one, and contiguous policy must raise
  `ContinuityError`.

Both receipts must report a durable spool state of `failed`, no completed
recording, no publication, and no retained `.partial` directory. Missing-frame
rejection is therefore based on capture metadata rather than guessing from IQ.

## Report identities and limits

The canonical report includes exact recording data/metadata references, truth
objects, FeatureSet references, dataset snapshot and bundle identities,
threshold-rule identity/digest, evaluation reference/report object, aggregate
confusion and association matrices, resource totals, and failure receipts. Its
`result_digest` covers the complete nested result; the console prints the
SHA-256 of the full report envelope.

This proves interface composition, deterministic identity, local durability,
split isolation, and two capture failure modes. It does not establish real-noise
accuracy, production-duration sensitivity, Starlink association, live-radio
safety, PostgreSQL durability, shared-storage behavior, or cross-host operation.
