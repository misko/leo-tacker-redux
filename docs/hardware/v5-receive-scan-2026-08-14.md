# V5 receive-only edge scan: 2026-08-14

This record covers one bounded live execution of the immutable
`plan_v5_scan_20260814_v1` against `ip:192.168.1.15`. It used the existing V5
capture path, a fresh local ext4 output root, local filesystem CAS, an in-memory
catalog, and local analysis consumption. It made no TX call and attempted no
PostgreSQL, NFS, or other off-host write.

## Preflight

| Gate | Observation |
|---|---|
| Exclusive clients | V5 canary and scan units inactive; no existing TCP connection to `.15` |
| Host time | `NTPSynchronized=yes` |
| Output filesystem | `/dev/mapper/ubuntu--vg-ubuntu--lv`, ext4 |
| Reachability | 2/2 ICMP replies, 0% loss, 0.208 ms average RTT |
| Serial | `104000b29905000e17000800065934759d` |
| Firmware | `v0.38-plutoplus-spf-libiio-metadata-v5` |
| Runtime | `pluto-v5-libiio-0.25-spfmeta3` attestation passed |
| TX2 before capture | `-80 dB`; DDS `altvoltage4..7` all zero |

The qualified runtime was image
`sha256:9f2424b29f89fd73fd33a64828056f911f68355eb67950647e4c6d788ca7d766`.
The current source at commit `1fb2f9f` was mounted read-only. The only writable
mount was the newly created local output directory.

## Exact live command

```text
docker run --rm --network host --read-only \
  --cap-drop ALL --cap-add DAC_OVERRIDE \
  --security-opt no-new-privileges \
  --mount type=bind,src=/home/mouse9911/gits/leo-tracker-redux/src,dst=/workspace/src,readonly \
  --mount type=bind,src=/var/tmp/leo-v5-rx-e2e-20260814.tSRJlQ,dst=/run/e2e \
  --env PYTHONPATH=/workspace/src \
  --env PYTHONDONTWRITEBYTECODE=1 \
  leo-flow-v5:canary-v2 \
  /usr/bin/python3 -m leo_flow.deployments.v5_scan_e2e \
  --live --output-root /run/e2e \
  --confirm-radio-serial 104000b29905000e17000800065934759d
```

The command exited zero after 11.89 seconds. Capture itself ran from UTC ns
`1786750337671282774` through `1786750345798029029` (about 8.13 seconds).

## Capture and frame accounting

| Segment | Actual center (Hz) | Samples per RX | Refills | Buffer seq. | Missing buffers | Missing samples | Flags |
|---|---:|---:|---:|---:|---:|---:|---|
| ch1 lower | 959,687,498 | 262,144 | 1 | 0 | 0 | 0 | none |
| ch1 upper | 1,190,312,500 | 262,144 | 1 | 0 | 0 | 0 | none |
| ch2 lower | 1,209,687,498 | 262,144 | 1 | 0 | 0 | 0 | none |
| ch2 upper | 1,440,312,500 | 262,144 | 1 | 0 | 0 | 0 | none |
| ch3 lower | 1,459,687,498 | 262,144 | 1 | 0 | 0 | 0 | none |
| ch3 upper | 1,690,312,498 | 262,144 | 1 | 0 | 0 | 0 | none |
| ch4 lower | 1,709,687,500 | 262,144 | 1 | 0 | 0 | 0 | none |
| ch4 upper | 1,940,312,500 | 262,144 | 1 | 0 | 0 | 0 | none |

All eight manifests report the exact requested paired shape
`262144 x 2 x 2`, signed little-endian CI16, at an actual sample rate of
2,083,331 samples/s. Each stored segment is covered completely by one V5
metadata refill and has `verified_contiguous` status, no continuity gaps, no
overflow counters, and no failure flags.

This evidence has an important limit: the adapter recreates the stream for
each tuning. Each segment consequently has a new stream ID and buffer sequence
zero. There is no same-stream predecessor for its first refill. Therefore zero
missing buffers/samples is proven within every stored segment, while sequence
deltas across tunings are not meaningful loss measurements; they are retune
boundaries. The one-refill scan qualifies complete bounded scan segments, not
sustained multi-refill continuity. The separate 30-refill canary remains the
evidence for that behavior.

## Integrity and local handoff

| Artifact | Bytes | SHA-256 | Result |
|---|---:|---|---|
| Paired IQ | 16,777,216 | `7fd0b9d0a16b4dc00fab68c83b93660eec1eec824d1a8d977876270b99f92365` | Recomputed; matches CAS identity and manifest extent |
| SigMF metadata | 28,660 | `9d81565198acafcbe67f60871b93c7b135f548219b81e9630c18c878f0e6e15f` | Recomputed; canonical metadata parsed successfully |
| Local feature bundle | 56,719 | `110ed7017b450c65eb419ded8ba03c05033ea1b13a4a40693cf5f56014025f09` | Readable after recording submission |
| E2E report | 2,453 | `fc566e0b2ac7628460b3467e487643ff61c224313f317fea987c94e1579b7e40` | Local run receipt |

Recording ID is `rec_01M019X0KZK9JEPWPYATZ7SGTX`, manifest digest is
`sha256:e10e82d1bbe3e72b245d5bfd3c0621eb9b3590913a115603bc1128c65ced3d46`,
and recording identity digest is
`sha256:a6420a620eeadac992bc7530cc1a6e570b69352b9caea11ccba861400954516c`.

Local publication reported one published and cleaned recording, zero deferred
recordings, and a reopened SQLite spool retained the durable-plan admission
gate. The finalized per-recording staging directory was empty after CAS
publication. Local analysis then read the CAS recording and published one
feature bundle with no warnings or reason codes. This proves the bounded
radio-to-local-CAS handoff and readback path; it does not qualify PostgreSQL,
NFS, or off-host transfer.

TX2 was checked again after capture and remained at `-80 dB` with all four DDS
scales zero. The preserved local evidence root is
`/var/tmp/leo-v5-rx-e2e-20260814.tSRJlQ`.
