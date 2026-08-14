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

This remains a plumbing and detector-behavior benchmark, not a promotion claim.
Its noise is deterministic uniform synthetic noise, its signal is only the
published coded edge-pilot subset, its impairments are simple and stationary,
and the locked-test labels are held out from fitting but are not sealed or
blinded. Hardware safety, RF behavior, real receiver noise, capture continuity,
shared CAS/PostgreSQL, and cross-host operation require separate qualification.
