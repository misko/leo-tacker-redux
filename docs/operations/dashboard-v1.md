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

Detector evaluation summaries are available at
`GET /api/evaluations/{identity}`, where `identity` is an immutable `eval_`
evaluation ID or `erun_` execution ID. The versioned response contains bounded
method/split coverage, firing and confusion counts, warnings, and the exact
canonical report `ObjectRef`. Covariance matrices remain in that canonical
report; the JSON route neither opens the report nor exposes a filesystem path.

## Offline public-boundary proof

`tests/application/test_public_projection_dashboard_e2e.py` is the deterministic
no-service proof for the complete read-model boundary. It constructs only public
contract values, validates exact published recording, FeatureSet, and model
references through the projection-command gates, then queries the resulting
recording status, feature, model, and detector-evaluation views through
`DashboardQueryPort` and the JSON application. It opens no raw IQ, CAS object,
database, mount, or radio.

The proof also fixes the compatibility behavior expected of concrete projection
adapters:

- replaying identical immutable inputs leaves every reduced query result
  unchanged;
- mismatched recording/FeatureSet/model identities fail before a reduced row is
  exposed;
- a recording whose analysis is incomplete remains visible with its explicit
  `pending` state while feature, model, and evaluation queries remain empty or
  not found;
- unsupported authoritative schema versions fail during contract construction;
  and
- a pagination cursor retains its original high-water snapshot when later rows
  arrive, while reuse with a different query fingerprint fails closed.

The dashboard package is separately architecture-tested to import neither
capture/analysis engines nor the recording codec. Its read model contains no IQ
reader operation, authoritative FeatureSet/ModelSnapshot bundle, or provenance.
The evaluation route carries only its explicit public report `ObjectRef`, whose
locator is required to be the canonical `cas:sha256:` form; it never opens that
object or exposes a constructed filesystem path.

## Operator interface

The same process serves the operator interface at `/`, with packaged assets at
`/assets/dashboard.css` and `/assets/dashboard.js`. It uses the JSON routes
above on the same origin; there is no frontend daemon, filesystem report scan,
or additional credential. The default two-hour view sends integer UTC
nanosecond bounds as `[start_utc_ns, stop_utc_ns)`. Operators can select six or
24 hours and refresh without changing server-side state.

Models and detector evaluations are exact lookups because Dashboard v1 has no
mutable list/latest route for either entity. Enter an immutable `model_`,
`eval_`, or `erun_` identity, or an explicitly approved model release alias.
The current API does not expose typed LNB parameters, evaluation covariance,
satellite associations, or track covariance. The interface labels those gaps
and never opens report objects or invents values. Browser “stale” means no
successful local refresh for two minutes; it is not a claim about projection
age, which the current DTOs do not carry.

HTML and JSON responses are not cached. The non-fingerprinted CSS and
JavaScript assets may be cached for at most five minutes. Static responses use
a same-origin CSP and reject framing; the fixed allow-list does not translate a
request path into a local resource path.

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

For access from one trusted workstation without installing a proxy, keep the
listener on loopback and use a local SSH forward to `127.0.0.1:8090`. For a
shared operator endpoint, use a dedicated authenticated TLS virtual host whose
root proxies to the loopback listener. The interface uses absolute same-origin
paths, so do not mount it below a path prefix. A minimal Nginx location inside
an already secured virtual host is:

```nginx
location / {
    proxy_pass http://127.0.0.1:8090;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 10s;
}
```

Authentication, TLS certificates, request limits, and operator authorization
belong to that reverse proxy. Do not publish port 8090, forward credentials to
the browser, weaken the loopback bind, or cache `/api/` responses. Verify `/`,
one `/api/storage-health` request, authentication rejection, TLS, and the CSP
after proxy installation.

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
