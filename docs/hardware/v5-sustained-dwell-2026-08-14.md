# V5 sustained receive dwell: 2026-08-14

This record covers one bounded, receive-only, single-tuning dwell on
`ip:192.168.1.15`. The goal was to remove the retune boundary present in the
eight-segment scan and test frame continuity across multiple refills of one V5
stream. The run used fresh local ext4 storage, local filesystem CAS, an
in-memory catalog, and no analysis step. It attempted no TX, PostgreSQL, NFS,
or other off-host write.

## Immutable dwell

| Property | Value |
|---|---|
| Plan ID | `plan_v5_dwell_20260814_v1` |
| Plan digest | `sha256:bf55e04a414a3fed045d0f4aa627c10fc3fcbb66882e50f4841e7ae93d86054e` |
| Activity | One `ActivityKind.DWELL` |
| Tuning | 1,825,117,187.5 Hz requested; 1,825,117,186 Hz read back |
| Sample rate | 2,083,332 requested; 2,083,331 samples/s read back |
| Bandwidth | 2,000,000 Hz |
| Receivers | Paired RX1/RX2 |
| Hardware block | 262,144 samples per receiver |
| Bounded extent | 16 refills; 4,194,304 samples per receiver |
| Expected IQ bytes | 33,554,432 |
| TX | Prohibited by plan tag and absent from the harness |

Sixteen refills provide 15 same-stream transitions and about 2.013 seconds of
stored RF time. This is long enough to expose sequence discontinuities while
keeping the raw paired recording near 32 MiB.

## Preflight

| Gate | Observation |
|---|---|
| Competing clients | V5 canary and scan units inactive; no established TCP connection to `.15` |
| Host time | `NTPSynchronized=yes` |
| Output filesystem | `/dev/mapper/ubuntu--vg-ubuntu--lv`, ext4 |
| Reachability | 2/2 ICMP replies, 0% loss, 0.246 ms average RTT |
| Serial | `104000b29905000e17000800065934759d` |
| Firmware | `v0.38-plutoplus-spf-libiio-metadata-v5` |
| Qualified runtime | `pluto-v5-libiio-0.25-spfmeta3` passed |
| TX2 before capture | `-80 dB`; DDS `altvoltage4..7` all zero |

The runtime image was
`sha256:9f2424b29f89fd73fd33a64828056f911f68355eb67950647e4c6d788ca7d766`.
Current source was mounted read-only. The sole writable container mount was the
new local evidence directory.

## Exact command

```text
docker run --rm --network host --read-only \
  --cap-drop ALL --cap-add DAC_OVERRIDE \
  --security-opt no-new-privileges \
  --mount type=bind,src=/home/mouse9911/gits/leo-tracker-redux/src,dst=/workspace/src,readonly \
  --mount type=bind,src=/var/tmp/leo-v5-rx-dwell-20260814.BQ8LZn,dst=/run/dwell \
  --env PYTHONPATH=/workspace/src \
  --env PYTHONDONTWRITEBYTECODE=1 \
  leo-flow-v5:canary-v2 \
  /usr/bin/python3 -m leo_flow.deployments.v5_dwell_e2e \
  --live --output-root /run/dwell \
  --confirm-radio-serial 104000b29905000e17000800065934759d
```

The command exited zero in 4.34 seconds. The report's capture wall interval was
2,985,479,391 ns. The metadata-covered stream interval was 2,013,202,818 ns,
and the ideal duration of the stored sample count at the actual rate was
2,013,268,175 ns.

## Same-stream continuity

| Evidence | Result |
|---|---|
| Stream IDs | One: `91745361387356` |
| Refill indexes | 0 through 15 |
| Buffer sequences | 0 through 15 |
| Buffer deltas | Fifteen values, all exactly 1 |
| First FPGA sample sequence | `1930894624581` |
| Final sequence, exclusive | `1930898818885` |
| FPGA sample deltas | Fifteen values, all exactly 262,144 |
| Stored sample offsets | Complete contiguous coverage of 4,194,304 samples per receiver |
| Missing buffers | 0 |
| Missing samples | 0 |
| Continuity gaps | 0 |
| Refill flags | None |
| Gain-observation overflow | 0 |
| FPGA gain-event overflow | 0 |
| Continuity status | `verified_contiguous` |

Unlike the scan result, these are true same-stream transitions: there was no
retune or stream recreation between refills. Missing-frame accounting is
therefore meaningful across every one of the 15 observed boundaries.

## Throughput and integrity

| Measurement | Result |
|---|---:|
| Required paired-IQ payload | 16,666,648 bytes/s |
| Payload over metadata stream span | 16,667,189 bytes/s |
| Payload over whole capture wall interval | 11,239,211 bytes/s |

The metadata-span rate matches the required paired-IQ rate. The whole-capture
rate is lower because its denominator also includes initial device
configuration, tuning, buffer creation, and finalization; it is not evidence of
steady-state transport loss.

| Artifact | Bytes | SHA-256 | Validation |
|---|---:|---|---|
| Paired IQ | 33,554,432 | `4ee6951214c6bccc18029bbac27b295e3136351e040e0f4046d379cdc6aa70c9` | Recomputed; exact manifest extent |
| SigMF metadata | 24,971 | `d8bddabc0f734b1a17c59074ce7620ae0c096dde03fac4bba5ad55fb8d73a0d7` | Recomputed; canonical pair read successfully |
| E2E report | 3,635 | `751e8f1b72b6515031c1e3fdb9d8cffcde51619733a1736a35588ab3ed271b49` | Preserved local receipt |

Recording ID is `rec_01M01E52YBHKATF5M986YXT2C9`, manifest digest is
`sha256:4a599151de0ec41346f8f5abc762a784a798ec01f4b86fde4ca4788db2380004`,
and recording identity digest is
`sha256:98efa0264e6111ab5f090728bdb8ef68823a5230f371172e987d5c34f874fddf`.

Local publication reported one published and cleaned recording, zero deferred
recordings, and an empty publication queue after reopening SQLite. The reopened
spool retained the exact plan's durable-recording admission gate, preventing
recapture. The staging directory was empty after CAS publication. This proves
local handoff and restart safety only; PostgreSQL, NFS, and off-host transfer
were deliberately not exercised.

TX2 remained at `-80 dB` with all DDS scales zero after capture, and no TCP
connection remained. Evidence is preserved at
`/var/tmp/leo-v5-rx-dwell-20260814.BQ8LZn`.

## Limits

This is a roughly two-second transport/continuity qualification, not an
8–24-hour soak. The first captured refill has no stored predecessor, so the 15
subsequent transitions—not time before capture admission—form the loss
denominator. With no LNB or known injected signal, the samples provide no
detection ground truth, sensitivity calibration, satellite association, or
dwell-selection evidence.
