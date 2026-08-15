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
| `deploy/offline-analysis-v1/leo-offline-analysis@.service.example` | `/etc/systemd/system/leo-offline-analysis@.service` |
| `deploy/dashboard-v1/leo-dashboard.service` | `/etc/systemd/system/leo-dashboard.service` |
| `deploy/operations-v1/leo-flow.target.example` | `/etc/systemd/system/leo-flow.target` |
| `deploy/operations-v1/leo-flow-health.{service,timer}` | `/etc/systemd/system/` |
| `deploy/operations-v1/health.example.json` | `/etc/leo-flow/systemd-health.json` |

Install the existing storage-capacity service/timer too. Copy the checked capture
and dashboard JSON files to the paths named by their units. Copy the analysis
JSON to `/etc/leo-flow/analysis-worker-1.json`, give it a unique `instance_id`,
and install the reviewed `leo_station.analysis_v1:PLUGIN`. Additional independent
workers use another `%i`, config file, instance ID, and corresponding health entry.

Run `systemd-analyze verify` over the materialized units before enabling them.
Validate all three component JSON files with `load_service_config`; validate the
health JSON against `health.schema.json` and with `systemd_health.load_config`.

## Ordering and isolation

```text
network-online + time-sync + successful storage-capacity check
    -> singleton capture supervisor
    -> independently restartable analysis worker instance(s)
    -> read-only dashboard
    -> periodic systemd health receipt
```

`After=` controls ordering only. Analysis does not require capture to remain
running, and dashboard does not require analysis to remain running. Capture and
analysis require the storage readiness check; both independently verify their
own external capabilities during service preflight. Stopping or restarting
`leo-flow.target` propagates through `PartOf=` to the long-running components.

Each process is locked by `flock` for its full lifetime under a private systemd
runtime directory. Capture remains mutually exclusive with its existing canary.
Automatic restart is only `on-failure`, with start-rate limits on all three
components. SIGINT/SIGTERM initiate the existing bounded drain, and systemd's stop
timeout remains longer than the configured application shutdown timeout.

The three database DSNs remain separate systemd credentials. They must grant only
the capture, analysis, and dashboard roles respectively. No secret belongs in a
JSON file, unit command line, health receipt, or journal message.

The storage-capacity and health timers use different oneshot services and may run
concurrently. systemd prevents overlapping invocations of each individual
service. Neither timer is a job queue or CAS control plane.

## Deterministic startup and recovery

After installing site inputs:

```console
systemctl daemon-reload
systemctl enable --now leo-flow.target
systemctl start leo-flow-health.service
systemctl status leo-flow.target --no-pager
```

For recovery, stop the aggregate, retain spool/CAS/catalog evidence, correct the
failed dependency, reset only the named failed units, then restart in the same
order:

```console
systemctl stop leo-flow.target
systemctl reset-failed leo-v5-scan.service \
  leo-offline-analysis@worker-1.service leo-dashboard.service
systemctl start leo-storage-capacity.service
systemctl start leo-flow.target
systemctl start leo-flow-health.service
```

Do not delete spool state, CAS objects, job leases, or receipts during recovery.
Capture publication and fenced analysis replay their existing idempotent paths.
Dashboard restart is read-only.

The health oneshot reads only bounded `systemctl show` properties. It writes one
atomic mode-0600 receipt to
`/var/lib/leo-flow/operations-health/latest.json` and emits the same canonical
JSON to the journal. Exit 0 is pass, 2 is an observed unhealthy state, and 3 is
a sanitized query/config/write failure. A receipt covers load/active/sub-state,
last result, exit status, restart policy/count, and both periodic timers. It is a
local observation, not proof of radio, PostgreSQL, or shared-storage correctness;
retain the existing off-host and hardware qualification receipts for those gates.

## Remaining site inputs

- reviewed capture plan/radio identity and approved analysis plugin;
- exact CAS mount/source, shared group/ACL, and capacity thresholds;
- three role-scoped credential source files and PostgreSQL endpoint;
- worker instance count and unique analysis configs/instance IDs;
- dashboard reverse proxy, authentication, TLS, and bind policy;
- alerting/retention destination for health JSONL and receipt history.
