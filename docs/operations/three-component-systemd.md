# Three-component systemd deployment

This package supervises the existing capture, offline-analysis, and dashboard
compositions. It adds ordering and health evidence, not another workflow engine.
No component imports another: systemd owns process ordering and each process still
communicates through the existing contracts, PostgreSQL catalog, and CAS.

## Materialize the examples

Install the reviewed files as follows:

| Repository example | Site path |
| --- | --- |
| `deploy/v5-scan/leo-v5-scan.service` | `/etc/systemd/system/leo-v5-scan.service` |
| reviewed `deploy/v5-scan/capture.json` | `/etc/leo-flow/v5-scan-capture.json` |
| `deploy/offline-analysis-v1/leo-offline-analysis@.service.example` | `/etc/systemd/system/leo-offline-analysis@.service` |
| reviewed `deploy/offline-analysis-v1/analysis.json` | `/etc/leo-flow/analysis-worker-1.json` |
| `deploy/dashboard-v1/leo-dashboard.service` | `/etc/systemd/system/leo-dashboard.service` |
| reviewed `deploy/dashboard-v1/dashboard.json` | `/etc/leo-flow/dashboard.json` |
| `deploy/operations-v1/leo-flow.target.example` | `/etc/systemd/system/leo-flow.target` |
| `deploy/operations-v1/leo-flow-health.{service,timer}` | `/etc/systemd/system/` |
| `deploy/operations-v1/health.example.json` | `/etc/leo-flow/systemd-health.json` |
| `deploy/storage-capacity/leo-storage-capacity.{service,timer}` | `/etc/systemd/system/` |
| reviewed `deploy/storage-capacity/capacity.example.json` | `/etc/leo-flow/storage-capacity.json` |
| `deploy/ephemeris-provider-canary/leo-ephemeris-provider-canary.{service,timer}` | `/etc/systemd/system/` |
| reviewed `deploy/ephemeris-provider-canary/huggingface-dry-run.example.json` | `/etc/leo-flow/ephemeris-provider-canary.json` |

Copy the checked capture and dashboard JSON files to the paths named by their
units. Copy the analysis JSON to `/etc/leo-flow/analysis-worker-1.json`, give it
a unique `instance_id`, and install the reviewed
`leo_station.analysis_v1:PLUGIN`. Additional independent workers use another
`%i`, config file, instance ID, and corresponding health entry. Install reviewed
capacity and offline ephemeris-canary JSON at the paths named by their services;
do not install the ephemeris network override for this offline deployment.

Run `systemd-analyze verify` over the materialized units before enabling them.
Validate all three component JSON files with `load_service_config`; validate the
health JSON against `health.schema.json` and with `systemd_health.load_config`.

## Ordering and isolation

```text
network-online + time-sync + successful storage-capacity check
    -> singleton capture supervisor
    -> independently restartable analysis worker instance(s)
    -> read-only dashboard

aggregate target -> capacity timer + offline ephemeris timer + health timer
```

| Unit | Required/wanted ordering | Failure coupling |
| --- | --- | --- |
| `leo-storage-capacity.service` | target requires successful completion | failed readiness prevents the ordered component start |
| `leo-v5-scan.service` | after storage readiness | no analysis import or runtime dependency |
| `leo-offline-analysis@worker-1.service` | after storage and capture completion | capture is ordering-only; each `%i` restarts independently |
| `leo-dashboard.service` | after the named analysis instance | analysis is ordering-only; dashboard remains independently restartable |
| three timers | wanted in parallel by the target | each invokes a distinct oneshot service |

`After=` controls ordering only. Analysis does not require capture to remain
running, and dashboard does not require analysis to remain running. Capture and
analysis require the storage readiness check; both independently verify their
own external capabilities during service preflight. Stopping or restarting
`leo-flow.target` propagates stop/restart through `PartOf=` to the components,
the three timers, and any currently running auxiliary oneshot.

Each process is locked by `flock` for its full lifetime under a private systemd
runtime directory. Capture remains mutually exclusive with its existing canary.
Automatic restart is only `on-failure`, with start-rate limits on all three
components. SIGINT/SIGTERM initiate the existing bounded drain, and systemd's stop
timeout remains longer than the configured application shutdown timeout.

The three database DSNs remain separate systemd credentials. They must grant only
the capture, analysis, and dashboard roles respectively. No secret belongs in a
JSON file, unit command line, health receipt, or journal message.

The storage-capacity, offline ephemeris, and health timers use different oneshot
services and may run concurrently. systemd prevents overlapping invocations of
each individual service. No timer is a job queue or CAS control plane. The
checked ephemeris unit permits only `AF_UNIX`; enabling network remains a separate
operator-reviewed action outside this offline package.

## Isolated rehearsal

`tests/services/test_operations_systemd_rehearsal.py` materializes every unit in
a temporary search path and runs `systemd-analyze verify` without installing,
starting, or stopping host units. Static unit/config assertions cover:

- successful storage-to-capture-to-analysis-to-dashboard ordering;
- two `%i` analysis instances with distinct configs, state, runtime, and locks;
- component-scoped credential sources and bounded graceful-stop timeouts;
- independent capacity, offline ephemeris, and health timer lanes;
- exact target ownership, restart policies, and timer-to-service wiring.

Separate deterministic tests feed synthetic `systemctl show` property maps into
the health qualifier. They cover healthy, failed, restarted, start-limit,
never-run, stale, invalid-timestamp, and reset-like observations, plus receipt
preservation when a later probe fails. They do not crash, restart, or reset a
real service manager. Actual manager lifecycle execution remains a required
site qualification before deployment.

Run only the isolated rehearsal with:

```console
.venv/bin/pytest -q tests/services/test_operations_systemd_rehearsal.py
```

## Deterministic startup and recovery

After installing site inputs:

```console
systemctl daemon-reload
systemctl enable --now leo-flow.target
systemctl start leo-ephemeris-provider-canary.service
systemctl start leo-flow-health.service
systemctl status leo-flow.target --no-pager
```

For recovery, stop the aggregate, retain spool/CAS/catalog evidence, correct the
failed dependency, reset only the named failed units, then restart in the same
order:

```console
systemctl stop leo-flow.target
systemctl reset-failed leo-v5-scan.service \
  leo-offline-analysis@worker-1.service leo-dashboard.service \
  leo-storage-capacity.service leo-ephemeris-provider-canary.service \
  leo-flow-health.service
systemctl start leo-storage-capacity.service
systemctl start leo-flow.target
systemctl start leo-ephemeris-provider-canary.service
systemctl start leo-flow-health.service
```

Do not delete spool state, CAS objects, job leases, or receipts during recovery.
Capture publication and fenced analysis replay their existing idempotent paths.
Dashboard restart is read-only.

Before `reset-failed`, copy `latest.json` to a mode-0600 incident path chosen by
the operator and retain the relevant component, capacity, ephemeris, and health
journal ranges under the site's evidence policy. A health probe/config/write
error exits 3 without replacing the last valid receipt.

```console
install -m 0600 /var/lib/leo-flow/operations-health/latest.json \
  /var/lib/leo-flow/operations-health/incident-REPLACE_WITH_APPROVED_ID.json
```

The health oneshot reads only bounded `systemctl show` properties. It writes one
atomic mode-0600 receipt to
`/var/lib/leo-flow/operations-health/latest.json` and emits the same canonical
JSON to the journal. Exit 0 is pass, 2 is an observed unhealthy state, and 3 is
a sanitized query/config/write failure. A receipt covers load/active/sub-state,
last result, exit status, restart policy/count, the latest capacity and offline
ephemeris oneshot results, and all three periodic timers. It is a local
observation, not proof of radio, PostgreSQL, or shared-storage correctness;
retain the existing off-host and hardware qualification receipts for those gates.

Oneshot success also requires nonzero, internally ordered
`ExecMainStartTimestampMonotonic`/`ExecMainExitTimestampMonotonic` evidence from
the current boot. The exit must be no older than the configured bound: one hour
for capture, ten minutes for capacity, and seven hours for the six-hour
ephemeris canary. A never-run, future-dated, or stale oneshot fails health even
when its default `Result` is `success`. Run the offline ephemeris canary once
before the first health qualification; timer activation alone is insufficient.

## Remaining site inputs

- reviewed capture plan/radio identity and approved analysis plugin;
- reviewed offline ephemeris-canary config and receipt retention policy;
- exact CAS mount/source, shared group/ACL, and capacity thresholds;
- three role-scoped credential source files and PostgreSQL endpoint;
- worker instance count and unique analysis configs/instance IDs;
- dashboard reverse proxy, authentication, TLS, and bind policy;
- alerting/retention destination for health JSONL and receipt history.
