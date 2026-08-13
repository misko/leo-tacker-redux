# Dashboard v1 deployment

Dashboard v1 is the first concrete production-shaped Redux process assembly. It
serves the existing JSON dashboard API over a stdlib HTTP listener and reads the
existing PostgreSQL dashboard projections. It has no capture, analysis, job, or
projection-write capability.

| Boundary | Exact selection |
|---|---|
| plugin | `leo_flow.deployments.dashboard_v1:PLUGIN` |
| query projection | `dashboard.postgres-projection-v1` |
| HTTP server | `dashboard.stdlib-loopback-http-v1` |
| secret provider | `systemd-credential` |
| credential name | `catalog-dsn` |

The example [configuration](../../deploy/dashboard-v1/dashboard.json) contains
only the named credential reference. The DSN belongs in the source file named
by `LoadCredential` in the example
[systemd unit](../../deploy/dashboard-v1/leo-dashboard.service); it must never
be placed in JSON, command-line arguments, or logs. systemd exposes the loaded
file through its process-specific `CREDENTIALS_DIRECTORY`. The provider has no
fallback to a general environment variable or conventional secret directory.

Install the package with its `server` optional dependency, install the example
configuration at `/etc/leo-flow/dashboard.json`, and adjust the credential
source path in the unit. The database login must be able to assume only the
`leo_dashboard` role established by the PostgreSQL migrations. Each query also
verifies that its transaction is read-only.

The v1 HTTP adapter accepts only an explicit loopback address. Put an
authenticated, TLS-terminating reverse proxy in front of it for remote access;
do not change the bind address to a wildcard. `serve_once` waits at most 250 ms,
which lets the service loop observe shutdown promptly. SIGTERM stops admission,
then the lifecycle calls the bounded listener close hook.

Readiness is stricter than a successful socket bind. Preflight also executes the
read-only storage-health query, proving database connectivity, assumption of the
`leo_dashboard` role, projection-table access, and read-only transaction mode.
If that query fails, preflight closes the newly bound listener before reporting
startup failure, so an unavailable database cannot leave a misleading open
port. Connection, statement, and lock waits have finite five-second bounds.

For a local one-request smoke test under a systemd credential environment:

```console
python -m leo_flow.services \
  --config /etc/leo-flow/dashboard.json \
  --plugin leo_flow.deployments.dashboard_v1:PLUGIN \
  --once
```

One-shot mode intentionally exits after one request or one finite idle wait.
Importing the plugin reads no credential, imports no optional PostgreSQL driver,
opens no database connection, and binds no socket. Those actions occur only
after complete bootstrap validation, during credential resolution, adapter
selection, listener preflight, and request handling respectively.
