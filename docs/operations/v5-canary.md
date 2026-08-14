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

## Installation and operation

Install the V5 runtime image, copy `capture.json` to
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

Success emits `ready`, `unit_completed`, and `stopped` JSONL events. A later
manual start should emit readiness and stop without a completed unit because
the exact plan is already durable. Do not delete or edit the SQLite database to
force a rerun; qualify a new immutable plan/plugin revision instead.
