# One-shot V5 capture canary

This deployment runs one exact, passive receive-only `ActivityKind.TEST` plan
against the qualified V5 radio at `192.168.1.15`. It contains no scheduler,
inbox, NFS marker, analysis import, or transmit operation. The plan is one
262,144-sample paired RX refill at 2.083332 MS/s and uses
`ALLOW_VERIFIED_GAPPED`; it therefore preserves any V5 sequence-gap evidence
instead of claiming contiguous IQ.

The service is intentionally `--once`. A clean first run captures, finalizes a
root-confined SigMF pair, uploads both objects to the local filesystem CAS,
atomically publishes the pair to PostgreSQL, and writes the capture-owned
recording/activity projections. A successful rerun observes the durable plan
row and performs no capture.

## Durable layout

| Purpose | Location | Ownership |
|---|---|---|
| SQLite recovery state | `/var/lib/leo-flow-v5-canary/capture-spool.sqlite3` | capture process only |
| Final/partial SigMF pairs | `/var/lib/leo-flow-v5-canary/recordings` | capture process only |
| Content-addressed objects | `/var/lib/leo-flow-v5-canary/cas/sha256/...` | storage adapter only |
| Single-instance lock | `/run/leo-flow-v5-canary/instance.lock` | process lifetime |
| Runtime attestation manifest | `/opt/leo-v5/runtime-manifest.json` | immutable image |

No constructed path is written to PostgreSQL except the CAS adapter's opaque
`cas:sha256:...` locator. Local SigMF files are deleted only after catalog and
projection publication succeeds.

## Startup and restart order

1. Acquire a nonblocking process-lifetime file lock and create only the
   systemd-owned state subdirectories.
2. Require at least 1 GiB free on each configured local root.
3. Open the SQLite spool. Recover any finalized pair left between rename and
   SQLite completion, or quarantine an unfinished `.partial` allocation.
   Malformed finalized data fails startup for operator inspection.
4. Prove the PostgreSQL `leo_capture` role with finite connect, statement, and
   lock timeouts.
5. Only when the plan has no durable recording, attest the current process's
   pinned libiio/pyadi/SPF runtime and the selected radio, then expose the radio
   to capture. Publication-only retries do not reopen the radio.

If CAS upload, catalog insertion, projection, SQLite acknowledgement, or local
cleanup fails, the unit exits nonzero. systemd restarts it and reconciliation
uses the same content identities and publication key. A `complete`,
`acknowledged`, or `cleaned` spool row prevents recapture. Capture failures are
durably marked `failed` and may be retried as a new recording.

Shutdown is idempotent. The radio's finite 5-second libiio timeout bounds normal
I/O; the service lifecycle gives its close hook 10 seconds and systemd gives the
process 12 seconds. The close path releases the radio before the instance lock.

`time-sync.target` is ordered before the host service. That is a supervision
dependency, not proof that UTC is disciplined: the operator must separately
verify the host's synchronization state. V5 refill metadata records realtime,
monotonic time, and measured uncertainty; `clock_status` describes this source
and does not claim a GNSS or PTP lock.

## Hardware-free rehearsal

Run the deployment-owned synthetic rehearsal before creating a live unit. It
uses a temporary SQLite spool and SigMF pair, a fake metadata-aware radio, and a
fake publisher. It captures once, restarts, proves that the durable plan is not
recaptured, prints one JSON result, and performs no credential, PostgreSQL,
pyadi, socket, or radio operation.

```console
python3 -m leo_flow.deployments.v5_canary_dry_run
```

The same command is installed in the V5 image:

```console
docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  leo-flow-v5:qualified \
  /usr/bin/python3 -m leo_flow.deployments.v5_canary_dry_run
```

The expected result has `status:"pass"`, one capture admission, zero restart
capture admissions, and one publication. This does not attest a radio or prove
database reachability; those remain live preflight gates.

## Installation and operation

For a host-installed runtime, install the qualified V5 filesystem, copy `capture.json` to
`/etc/leo-flow/v5-canary-capture.json`, copy `leo-v5-canary.service` into the
systemd unit directory, and provide `/etc/leo-flow/secrets/capture-catalog-dsn`.
The credential's database principal must be a member of `leo_capture`; the DSN
is loaded through `LoadCredential` and never appears in configuration or logs.

Before enabling the unit, verify the documented RX1/RX2 tee fixture and confirm
that no transmit software is active. Then run:

```console
systemctl daemon-reload
systemctl start leo-v5-canary.service
journalctl -u leo-v5-canary.service -o cat
```

The built container instead carries its exact config at
`/opt/leo-v5/deploy/v5-canary-capture.json`. Do not also enable the host unit.
For a later authorized live run, create the two host directories below first;
their bind mounts make both durable state and the singleton lock common to all
container attempts:

```console
install -d -m 0700 /var/lib/leo-flow-v5-canary /run/leo-flow-v5-canary
docker run --rm --name leo-v5-canary \
  --network host --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --mount type=bind,src=/var/lib/leo-flow-v5-canary,dst=/var/lib/leo-flow-v5-canary \
  --mount type=bind,src=/run/leo-flow-v5-canary,dst=/run/leo-flow-v5-canary \
  --mount type=bind,src=/etc/leo-flow/secrets/capture-catalog-dsn,dst=/run/catalog-dsn,readonly \
  --env CREDENTIALS_DIRECTORY=/run \
  leo-flow-v5:qualified \
  /usr/bin/python3 -m leo_flow.services \
  --config /opt/leo-v5/deploy/v5-canary-capture.json \
  --plugin leo_flow.deployments.v5_canary:PLUGIN --once
```

The bind-mounted `/run/leo-flow-v5-canary` is mandatory. Replacing it with a
container-private tmpfs would defeat cross-container singleton enforcement.

Success emits `ready`, `unit_completed`, and `stopped` JSONL events. A later
manual start should emit readiness and stop without a completed unit because
the exact plan is already durable. Do not delete or edit the SQLite database to
force a rerun; qualify a new immutable plan/plugin revision instead.

## Authorized live TEST checklist

Do not start the host unit or live container until every item is satisfied:

- The exact image digest and runtime verifier output match the qualification
  record; the hardware-free rehearsal passes.
- The host reports synchronized UTC, and its clock did not step during the
  rehearsal window.
- `192.168.1.15` is the intended serial
  `104000b29905000e17000800065934759d` on an isolated/trusted station network.
- RX1 and RX2 are connected to the documented TX2 SMA tee, no LNB is attached,
  and independent inspection confirms that no transmitter or DDS service is
  active. This plan contains no TX operation.
- The state and runtime directories are real local directories, not NFS or
  symlinks; at least 1 GiB is free; no canary process/container is running.
- The credential file is single-line, root-readable only, and names a principal
  that can `SET ROLE leo_capture`; migrations through `0008` are applied.
- The operator has recorded the pre-run state directory listing and journal
  cursor and has a stop/rollback path that preserves SQLite, quarantine, and CAS
  contents for diagnosis.
- Authorization explicitly covers one `ActivityKind.TEST` receive canary. A
  second scientific capture requires a new immutable plan revision, not spool
  deletion.
