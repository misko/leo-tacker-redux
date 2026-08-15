# Synchronized real-recording development audit v1

This benchmark is a bounded, read-only check of current detector behavior on a
small, score-blind subset of `/mnt/qnap01/mouse9911/leo-scans`. The repository
stores only a compact content-addressed manifest. It does not copy or commit IQ.

## Read-only inventory

The inventory was taken from the mount on 2026-08-14. `sweep.json` is the
documented commit marker. All 7,054 metadata files parsed as
`leo-tracker.interim-synchronised-scan/v1`, and every successful declared IQ
object existed with its declared size.

| Item | Observed count |
| --- | ---: |
| Sweep directories / `sweep.json` files | 7,054 |
| CI16 files | 13,215 |
| Total files, including root README | 20,270 |
| Total bytes | 1,141,277,703,024 |
| Successful radio attempts | 13,215 of 14,108 |
| Failed radio attempts, all `pluto-5d4d` | 893 |
| Matched / unmatched arm sweeps | 6,364 / 690 |
| UTC span in directory names | 2026-08-14 00:03:15–14:49:22 UTC |

Of the 893 failed attempts, 892 declare `KeyError: 'pluto-5d4d'` and one
declares `OSError: [Errno 5] Input/output error`; none has an IQ reference.
The inventory’s canonical metadata digest is
`22d47093ddb5241349236c42f968a9809c88419b1124c3d5316a8218d4a4df4b`.

The source README documents little-endian raw int16 with shape
`(tuning, sample, receiver, component)`, I/Q component order, two concurrently
run Pluto SDRs, a per-tuning barrier, radio-specific tuning order, local-NVMe
staging followed by verified copy, and metadata-last publication. Its
`skew_ms` is a barrier-release lower bound, not actual sample-start skew. The
README also records an operator-observed `lnb-a` hardware concern. That note is
useful quality context but is not independently verified truth.

## Truth and leakage boundary

The source metadata has no target labels, independent instrument observations,
TLE associations, injections, negative controls, pass IDs, or independently
established session IDs. A detector firing is not a positive label, and a
non-firing is not a negative label. Cross-radio agreement is only a replication
proxy. Consequently every member is `unlabeled_sky`, has
`target_present: null`, is accuracy-ineligible, and remains in the
`development` partition.

All six selected recordings share one split group for the entire UTC day.
This deliberately prevents train/validation/test carving from dense adjacent
sweeps or synchronized radios. A future independently evidenced session/pass
catalog can define new groups in a new manifest; this v1 manifest must not be
reinterpreted.

## Frozen selection and format

Selection uses metadata only. In directory order it requires both radios to
succeed, matched arms, exact `80ms-2.50MSps` arms whose pilot band fits, UTC
before `20260814T032400Z`, and declared maximum barrier skew no greater than
0.1 ms. There are 62 eligible sweeps. V1 takes the earliest opposite-order,
same-L, and same-U sweep, then the four starts 0, 65,536, 130,368, and 195,904
from each of eight tunings.

| Scope | Frozen value |
| --- | ---: |
| Sweeps / radio recordings | 3 / 6 |
| Referenced external IQ | 76,800,000 bytes |
| Detector window attempts | 192 |
| IQ read by detector | 6,291,456 bytes |

The manifest is
[`qnap-synchronised-real-development-v1.json`](manifests/qnap-synchronised-real-development-v1.json)
and its structural schema is
[`qnap-synchronised-real-dataset-v1.schema.json`](specs/qnap-synchronised-real-dataset-v1.schema.json).
`benchmark.qnap_real_dataset` additionally enforces cross-field invariants,
membership hashing, one-group development-only policy, source metadata
consistency, safe relative paths, and byte/hash checks.

## Observed detector behavior

The current `independent-detector-suite@0.1.0` runs with its frozen 4,096-sample
matrix configuration. Synthetic TRAIN-calibrated thresholds are applied only
to describe firing behavior; no threshold is fitted on these recordings.

| Method | Accepted windows | Firings | Firing fraction of accepted |
| --- | ---: | ---: | ---: |
| coarse-energy@0.1.0 | 123 | 5 | 0.0407 |
| paired-common-mode@0.1.0 | 123 | 0 | 0 |
| periodic-coherence@0.1.0 | 123 | 33 | 0.2683 |

The detector refused 69 of 192 window attempts because one or more components
met its absolute clipping threshold of 2,048. All refused windows are in the
three `pluto-19f2` recordings (22, 23, and 24 windows); the selected windows
contain respectively 58, 65, and 62 threshold-reaching components. This is a
quality-policy result, not evidence about target presence.

On the 123 accepted shared windows, coarse-energy/periodic firing covariance is
0.00535396 and phi is 0.0611900. Paired-common-mode never fires, so its phi is
undefined. The full matrix and pairwise counts are emitted in the JSON report.

Direct cross-radio comparison is limited to the two same-order sweeps. Of 64
potential matched windows per method, 47 are unavailable due to the clipping
refusals above. Exact agreement among the remaining 17 is 16/17 for energy,
17/17 for paired-common-mode, and 11/17 for periodic coherence. The
opposite-order sweep is excluded because matching tuning indices observed
different edges; these small agreement counts are not independent truth or a
full-corpus rate.

## Reproduce

The command reads the mount but writes only the requested local report:

```text
PYTHONPATH=src:. python3 -m benchmark.qnap_real_dataset \
  --root /mnt/qnap01/mouse9911/leo-scans \
  --verify-full-iq \
  --output /tmp/leo-flow-qnap-real-trackb-v1.json
```

The audited report is 10,992 bytes with SHA-256
`301ef396d6bca2ff8f6fc25534d0d8becb5996efde380ab424536f45c3ad97f4`.
Absolute tuning frequencies and segment timestamps in the adapter are
analysis-only reconstructions because the source metadata does not record
them; they must not be used as observational evidence.
