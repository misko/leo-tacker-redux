# Benchmark artifact specification v1

This directory defines evaluation inputs independently of capture, analysis,
and model implementations. It contains references and compact truth metadata,
not copied radio recordings.

## Corpus manifest

`leo-flow.benchmark-corpus/v1` has an ordered `members` array. Its
`membership_digest_sha256` is SHA-256 over the complete array encoded with
`leo-flow-benchmark-canonical-json-v1`: UTF-8 JSON, sorted object keys, compact
separators, ASCII escaping, and no floating-point values. The digest freezes
identity, partition, grouping, labels, and every source reference. Reordering
or changing any member is a new corpus.

Every object reference contains a storage root ID, relative location, byte
count, and exact SHA-256. Root IDs keep machine-specific mount points out of
scientific identity. A legacy recording contains many payload files, so its
`payload_set` also freezes the sorted list of `(path, bytes, sha256)` entries
declared by the exact source manifest. It does not pretend a directory path is
an object hash.

`split_group_id` is derived from station, capture session, UTC day, and pass
group. That conservative unit keeps simultaneous radios and adjacent windows
together. Radio, receiver-chain, and hardware-epoch keys remain explicit so an
evaluation making a hardware-generalization claim can additionally hold out a
complete epoch. A split group may occur in exactly one partition.

Development manifests are visible and use `development`, `train`, or
`validation` partitions. Locked-test manifests are separate, `sealed`, contain
only `locked_test` members, and do not expose labels to implementation agents.
Never turn the development manifest into a locked test by renaming it.

## Truth policy

The six accepted tiers are:

1. `exact_synthetic`
2. `exact_digital_injection`
3. `hardware_truth`
4. `independent_external_evidence`
5. `consensus_proxy`
6. `unlabeled_sky`

Legacy pipeline confirmation is tier 5 and cannot qualify a locked scientific
claim. An unfired or unconfirmed sky recording is tier 6, never a negative.
TLE agreement is not independent truth when TLE data participates in the
method under test.

## Legacy oracle

`leo-flow.legacy-oracle-summary/v1` records a small fixed set of values read
from exact legacy analysis and follow-up report hashes. These values are a
numerical regression oracle, not labels, ground truth, or a mandate to retain
legacy orchestration and thresholds. A deliberate algorithm change may differ
after its scientific acceptance criterion is documented.

## Synthetic IQ

`leo-flow.synthetic-iq-fixtures/v1` defines an integer-exact, detector-
independent QPSK NCO and xorshift noise generator. It carries exact frequency,
drift, SNR ratio/dB, receiver delay/gain/phase/DC, clipping, paired CI16 layout,
and normative byte hashes. It tests plumbing and estimator bookkeeping; it is
not a realistic Starlink waveform or a substitute for real-noise injection.

`benchmark.starlink_pilot_if` is the separate
`leo-flow.starlink-edge-pilot-if-fixture/v1` validation source. It generates a
selectable subset of the published 4QAM edge-pilot codes at the baseband offsets
seen by a receiver tuned to an edge-pilot-band center. It is still not a full
Starlink waveform. Its immutable truth bytes name the paper-derived pilot
indices, exact local offsets, level, optional deterministic digital noise,
frame-phase model, IF-center metadata, converter envelope, and CI16 digest.
The generator imports no detector or runtime pipeline component.

For example, explicitly materialize one two-pilot, three-frame lower-edge
fixture at 5 MS/s:

```text
python3 -m benchmark.starlink_pilot_if \
  --sample-rate 5000000 --sample-count 20000 \
  --edge lower --pilots 531,532 \
  --signal-rms 128 --if-center-hz 1709687500 \
  --output /tmp/starlink-two-pilot.ci16
```

This writes only the requested CI16 file and its adjacent `.truth.json`. Normal
callers should use the in-memory return value and persist an injection as a
content-addressed analysis artifact rather than building a directory workflow.

## Real synchronized development subset

`qnap-synchronised-real-development-v1.json` freezes six external CI16
recordings from three synchronized sweeps without copying IQ. All members are
unlabeled, accuracy-ineligible, development-only, and conservatively assigned
to one split group. The bounded reader and detector-behavior audit are described
in `qnap-synchronised-real-development.md`. They report firing covariance and
same-order radio agreement, never accuracy or inferred target labels.

## Validation

Run the dependency-free checks from the repository root:

```text
python3 -m benchmark.validate \
  benchmark/manifests/development-2026-08-13.json \
  --oracle benchmark/oracles/development-2026-08-13.legacy-summary.json \
  --synthetic-spec benchmark/specs/synthetic-iq-v1.json

python3 -m benchmark.synthetic_iq benchmark/specs/synthetic-iq-v1.json
```

If the legacy archives are mounted, verify all referenced metadata and payload
indexes without hashing multi-gigabyte IQ:

```text
python3 -m benchmark.validate benchmark/manifests/development-2026-08-13.json \
  --verify-files \
  --root legacy-qnap-leo=/mnt/qnap01/mouse9911/leo \
  --root legacy-qnap-leo-cropped=/mnt/qnap01/mouse9911/leo-cropped
```

`--verify-payloads` additionally hashes every IQ object and is intentionally a
manual audit. `--promotion-gate` fails until all declared scientific-promotion
coverage is present.
