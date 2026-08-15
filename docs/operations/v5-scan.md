# One-shot V5 edge scan

The V5 scan deployment captures one immutable, passive receive-only Starlink
edge scan on the radio at `192.168.1.15`. Production capture contains no
detector, analysis worker, scheduler, dwell decision, dashboard call, transmit
operation, or NFS marker protocol. It executes an already-materialized plan,
publishes the raw paired-RX recording, and exits.

The optional live E2E command documented below is an operator validation
harness. It deliberately composes local publication and a basic quality/PSD
analysis after capture to prove that the recording can cross the interface. It
does not change the production deployment boundary, and its quality output is
not a scientific detection or a dwell recommendation.

## Immutable scan contract

| Property | Exact value |
|---|---|
| Plan ID | `plan_v5_scan_20260814_v1` |
| Plan digest | `sha256:bf6947c46dbe06eaf9efcd2039785a1f432015610080c6e32965f1a58a560ab6` |
| Activity | One `ActivityKind.SCAN` |
| Receivers | RX1 and RX2, paired in every segment |
| Sample rate | 2,083,332 samples/s |
| Analog bandwidth | 2,000,000 Hz |
| Samples per segment | 262,144, exactly one qualified V5 refill |
| Segment order | Channel 1 lower/upper through channel 4 lower/upper |
| Segment count | Eight |
| Raw payload | 16,777,216 bytes across both receivers and all segments |
| LNB assumption in metadata | 9.750 GHz LO |
| Automatic dwell | Disabled |
| Analysis on capture host | Disabled in production |

The eight IF centers are 959.6875, 1190.3125, 1209.6875, 1440.3125,
1459.6875, 1690.3125, 1709.6875, and 1940.3125 MHz. They are explicit plan
values, not frequencies selected by capture-time code. Changing any tuning,
order, rate, sample count, receiver, experimental tag, or plan identity
requires a new plan ID, digest, and deployment revision. Do not edit the
embedded plan or delete spool state to repeat it.

## Production state and restart behavior

| Purpose | Location | Owner |
|---|---|---|
| SQLite recovery state | `/var/lib/leo-flow-v5-scan/capture-spool.sqlite3` | Capture process only |
| Final and partial SigMF pairs | `/var/lib/leo-flow-v5-scan/recordings` | Capture process only |
| Content-addressed objects | `/var/lib/leo-flow/objects/sha256/...` | Shared mounted CAS; capture publishes about two objects per recording |
| Single-instance lock | `/run/leo-flow-v5-scan/instance.lock` | Process lifetime |
| Runtime attestation manifest | `/opt/leo-v5/runtime-manifest.json` | Immutable image |

The service is intentionally invoked with `--once`. On its first successful
run it attests the pinned runtime and radio, acquires all eight segments,
atomically finalizes a SigMF data/metadata pair, publishes both objects and the
catalog projection, acknowledges the local spool, and exits. Once the plan has
a durable recording, a restart reconciles unfinished publication without
opening the radio or recapturing the plan.

A capture or publication failure exits nonzero. `Restart=on-failure` retries
after five seconds, within the configured start limit. Preserve the SQLite,
recording, quarantine, and CAS contents after a failure: they are recovery
state, not disposable scratch data.

Production refuses to start unless `/var/lib/leo-flow/objects` is a mounted
filesystem. The analysis host must mount the same authoritative CAS at that
exact root when constructing its station plugin. PostgreSQL carries immutable
recording and job references; it does not carry sample bytes. This bulk handoff
creates roughly one data object and one metadata object per recording, not one
file or marker per scan segment.

## Hardware-free gates

Run these before installing or starting the live unit:

```console
.venv/bin/pytest -q \
  tests/capture/test_scan_plan.py \
  tests/services/test_v5_scan_deployment.py \
  tests/services/test_v5_scan_e2e.py \
  tests/integration/test_v5_scan_e2e.py
```

These tests prove plan materialization, immutable identity, paired-RX capture,
SigMF finalization, publication, analysis submission, same-process quality/PSD
consumption, outage retry, and no recapture after restart. They use a fake
radio and do not contact `192.168.1.15` or prove the production shared mount.

## Install the production scan unit

Install only from the qualified V5 runtime/image. Copy
`deploy/v5-scan/capture.json` to `/etc/leo-flow/v5-scan-capture.json`, copy
`deploy/v5-scan/leo-v5-scan.service` into the systemd unit directory, and
provide `/etc/leo-flow/secrets/capture-catalog-dsn`. The database credential
must name the restricted capture publisher role and must not appear in JSON,
the command line, or logs.

The unit runs through `/opt/leo-v5/bin/runtime-entrypoint`, uses systemd-managed
state and runtime directories, and conflicts with the V5 canary unit. Before a
production run, provision the `leo-flow-cas` system group on both hosts and
mount the authoritative CAS at `/var/lib/leo-flow/objects`. The mount root must
be group-writable with setgid/default permissions that let both the capture and
analysis services create and read objects; the unit uses `UMask=0007` and joins
that group. A plain local directory is rejected. Verify the mount identity from
both hosts, then verify that PostgreSQL migrations and permissions are current:

```console
mountpoint -q /var/lib/leo-flow/objects
getent group leo-flow-cas
```

Start the capture only after those checks pass:

```console
systemctl daemon-reload
systemctl start leo-v5-scan.service
systemctl status leo-v5-scan.service --no-pager
journalctl -u leo-v5-scan.service -o cat
```

Success emits normal service lifecycle events and leaves one durable cataloged
recording for the exact plan. Starting the unit again must not reopen the radio
or create a second recording.

## Authorized live E2E on `.15`

This procedure performs real RX acquisition but no transmission. It is
appropriate for the current RX1/RX2-to-TX2 tee fixture with no LNB only when
TX2 is independently confirmed muted. Both the normal production radio
attestation and the harness read the exact radio serial and refuse to proceed
unless TX2 hardware gain is at or below -80 dB and all four TX2 DDS scales are
zero.

Before running it, verify all of the following:

- `192.168.1.15` resolves to serial
  `104000b29905000e17000800065934759d`.
- The qualified V5 runtime attestation passes and the host clock is
  synchronized without a step during capture.
- No canary, production scan, transmitter, DDS tool, or other libiio client is
  running.
- RX1 and RX2 still have the documented attenuated SMA tee connections and no
  LNB or antenna path is exposed to TX2.
- `/var/tmp/leo-v5-scan-e2e-20260814` is absent or completely empty and has
  enough local free space. It must not be NFS.

Run exactly one armed E2E attempt through the qualified entrypoint:

```console
install -d -m 0700 /var/tmp/leo-v5-scan-e2e-20260814
/opt/leo-v5/bin/runtime-entrypoint /usr/bin/python3 -m leo_flow.deployments.v5_scan_e2e \
  --live \
  --output-root /var/tmp/leo-v5-scan-e2e-20260814 \
  --confirm-radio-serial 104000b29905000e17000800065934759d
```

The serial argument is an explicit live-radio arm, not radio discovery. The
harness refuses a different serial or a nonempty output root.

## E2E evidence and interpretation

The command prints a one-line JSON report and writes the formatted report to
`/var/tmp/leo-v5-scan-e2e-20260814/e2e-report.json`. Keep that directory intact
until the run is reviewed.

| Evidence | Required result |
|---|---|
| `status` | `pass` |
| `mode` | `live-passive-rx-only` |
| `activity_kinds` | Exactly `["scan"]` |
| `segment_count` | `8` |
| `segment_sample_counts` | Eight values, each `262144` |
| `continuity` | All eight segments `verified` with no gaps |
| `frame_accounting` | One complete refill per segment, zero flags/gaps/missing counts; inter-segment accounting explicitly not applicable across retunes |
| `object_integrity` | Data and metadata digests verified; paired data extent exactly `16777216` bytes |
| `publication.published` | `1` |
| `publication.deferred` | `0` |
| `publication.restart_prevents_recapture` | `true` |
| `analysis` | One succeeded job and a readable feature set |
| `truth.kind` | `passive-no-lnb-baseline` |
| `truth.scientific_detection_claim` | `false` |

This passive no-LNB run establishes the radio-to-local-CAS data path,
continuity, serialization, and same-process analysis readability. It does not
establish off-host shared-CAS transport, PostgreSQL durability, a Starlink
detection false-positive rate, signal sensitivity, LNB calibration, satellite
association, or ground truth. Those require separate infrastructure
qualification plus versioned TX-fixture and antenna/LNB runs; any future dwell
remains a distinct, explicitly approved capture plan.
