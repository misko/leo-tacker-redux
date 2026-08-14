# Starlink synthetic detector matrix

This is an opt-in, bounded offline benchmark for the existing independent
detector suite. It composes the published-code edge-pilot fixture, paired-RX
impairment fixture, normal `IndependentDetectorSuite`, TRAIN-only threshold
calibration, frozen dataset snapshot, and `evaluate_detectors` contracts. It
does not open a radio, database connection, or network connection.

Run it from the repository root with the project virtual environment:

```text
.venv/bin/python -m benchmark.starlink_detector_matrix \
  --output /tmp/starlink-detector-matrix-report.json
```

The command writes only the explicitly requested report. Generated IQ,
FeatureSets, truth documents, and dataset objects remain in memory, so it does
not create one file per case or detector window. The report is canonical JSON:
UTF-8, sorted keys, compact separators, no non-finite floats, and one trailing
newline. Its console message gives the exact byte count, SHA-256, and runtime.
The SHA-256 identifies that emitted per-run report; it is not a reproducible
benchmark-result digest because the report intentionally contains measured
runtime fields. For fixed inputs and the same declared Python environment,
compare `benchmark_identity` and the scientific result fields across runs, not
the byte identity of the complete runtime-bearing report envelope.

The frozen v1 specification has 24 independently seeded background groups:
12 TRAIN, 6 validation, and 6 locked-test. Each group contains one null and
five counterfactual signal injections at -18, -10, -2, +6, and +14 dB nominal
source SNR. Matrix dimensions cover both pilot edges, inner/full published
pilot subsets, five CFO values, four target channels, and varied second-path
gain, phase, and integer delay. The +14 dB full-pilot arm exercises high
converter occupancy; fixture generation fails closed if any component clips.

Only TRAIN recordings enter threshold fitting. Validation and locked-test
scores and labels are passed to the threshold rule only after it is frozen.
The report contains recording- and segment-level confusion, per-SNR target
detection, achieved pre-quantization SNR by receiver, converter margin,
overall/per-split firing covariance and phi, the complete standard detector
evaluation, runtime and byte sizes, and explicit scientific limitations.
The per-SNR section records composite experimental arms: each nominal SNR is
bound to its declared pilot subset, CFO, edge flip, target-channel offset, and
near-clipping setting. It is not an isolated SNR response curve. The report
includes that arm mapping beside the rates so this constraint survives without
the separate specification document.

## Production-length representative-window profile

The optional frozen profile
`benchmark/specs/starlink-detector-representative-windows-v1.json` keeps the
same matrix membership and TRAIN/validation/locked-test assignments, while
representing each segment as a 3,200,000-sample logical recording. That length
is the largest shape in three read-only `sweep.json` metadata observations from
the existing QNAP corpus (100,000, 1,600,000, and 3,200,000 samples per
segment). The profile records the metadata hashes and shapes; the benchmark
does not read, copy, label, or analyze the associated real IQ.

Run the representative profile with:

```text
.venv/bin/python -m benchmark.starlink_detector_matrix \
  --representative-profile \
    benchmark/specs/starlink-detector-representative-windows-v1.json \
  --output /tmp/starlink-detector-representative-report.json
```

Four detector-independent positions are declared before execution: the start,
approximately one-third, approximately two-thirds, and the exact tail. Each
position receives independently seeded synthetic noise while preserving the
same null/injection background lineage inside a group. Across 144 recordings,
eight segments, and three methods this produces 4,608 aligned detector windows.
Only the selected windows are materialized: 150,994,944 IQ bytes are analyzed
in total, at most 1,048,576 IQ bytes per recording, while the logical corpus
represents 29,491,200,000 IQ bytes.

The profile fails closed before generation if recording, window, analyzed-byte,
materialized-byte, or logical-byte totals exceed its frozen limits. After a run
it also rejects output JSON, elapsed runtime, or process peak RSS above their
declared bounds. Peak RSS is the process high-water mark and includes the
interpreter and any earlier allocations in that process. The aggregate report
contains recording-, segment-, and window-level confusion, overall/per-split
firing covariance, phi, and exact agreement matrices, composite condition arms,
observed runtime/RSS, and the declared storage bounds.

This remains a plumbing and detector-behavior benchmark, not a promotion claim.
Its noise is deterministic uniform synthetic noise, its signal is only the
published coded edge-pilot subset, its impairments are simple and stationary,
and the locked-test labels are held out from fitting but are not sealed or
blinded. Hardware safety, RF behavior, real receiver noise, capture continuity,
shared CAS/PostgreSQL, and cross-host operation require separate qualification.
The representative profile adds temporal positions but does not scan
unselected time, and its windows within one recording are correlated rather
than independent long-duration observations. Its maximum-length sample count
comes from three metadata examples and is an engineering bound, not a
statistically representative corpus estimate; the synthetic matrix sample rate
also differs from the observed 5 MS/s arm with that shape.
