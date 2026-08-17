# Dashboard v1 deployment

Dashboard v1 is the first concrete production-shaped Redux process assembly. It
serves the existing JSON dashboard API over a stdlib HTTP listener and reads the
existing PostgreSQL dashboard projections. It has no capture, analysis, job, or
projection-write capability.

| Boundary | Exact selection |
|---|---|
| plugin | `leo_flow.deployments.dashboard_v1:PLUGIN` |
| query projection | `dashboard.postgres-projection-v1` |
| default HTTP server | `dashboard.stdlib-loopback-http-v1` |
| checked Gauss HTTP server | `dashboard.stdlib-explicit-remote-http-v1` |
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
eight hours, 24 hours, seven days, or 30 days and refresh without changing
server-side state. These remain bounded read-only queries and retain the same
half-open UTC semantics.

Models and detector evaluations are exact lookups because Dashboard v1 has no
mutable list/latest route for either entity. Enter an immutable `model_`,
`eval_`, or `erun_` identity, or an explicitly approved model release alias.
The current API does not expose typed LNB parameters, evaluation covariance,
satellite associations, or track covariance. The interface labels those gaps
and never opens report objects or invents values. Browser “stale” means no
successful local refresh for two minutes; it is not a claim about projection
age, which the current DTOs do not carry.

The same composition preserves every V1 route and adds the compatible batch
read API at `GET /api/v2/capture-batches` and
`GET /api/v2/capture-batches/{batch_id}`. The operator interface labels each
two-attempt batch as independent or coordinated, shows requested and observed
start skew, both terminal capture outcomes, per-recording analysis/result
availability, and explicit paired-analysis eligibility. Independent mode makes
no synchronization claim. Coordinated mode is measured software coordination,
not hardware synchronization.

Radio identity is data, not a fixed dashboard slot. A `.15` single-radio
recording can therefore appear in activity, recording detail, and FeatureSet
results while a `.20`/`.21` batch appears in the batch view at the same time.
The current Gauss identities are `radio_pluto_v5_canary_15`,
`radio_pluto_5d4d`, and `radio_pluto_19f2`; IP suffixes are operational labels,
not identities exposed by the dashboard contract.

### Multi-batch campaign boundary

The batch list follows its stable cursor and reports loaded capture-attempt,
analysis-terminal, and result-availability counts. That is sufficient to watch
the batches produced by a bounded campaign without private storage access. The
authoritative 3×3 sample-rate/duration balance remains in the campaign CLI and
journal: the published V2 batch DTO intentionally contains opaque `plan_id`
values and no campaign or matrix-cell identity. The dashboard must not infer
cell membership by opening private capture plans or parsing plan IDs.

HTML and JSON responses are not cached. The non-fingerprinted CSS and
JavaScript assets may be cached for at most five minutes. Static responses use
a same-origin CSP and reject framing; the fixed allow-list does not translate a
request path into a local resource path.

The default v1 HTTP adapter accepts only an explicit loopback address. A
separate `dashboard.stdlib-explicit-remote-http-v1` adapter makes a requested
non-loopback deployment auditable without weakening the default. It provides
the same read-only HTTP application but does not add authentication or TLS.
`serve_once` waits at most 250 ms, which lets the service loop observe shutdown
promptly. SIGTERM stops admission, then the lifecycle calls the bounded
listener close hook.

The checked Gauss development configuration currently selects that explicit
remote adapter on `0.0.0.0:8090` by operator request. It is consequently
reachable over every permitted IPv4 interface and must be treated as
unauthenticated cleartext telemetry. Do not copy this development choice to a
shared or untrusted network; use the authenticated TLS reverse-proxy pattern
below for production exposure.

### Durable Gauss all-interface cutover

The checked unit does not execute an editable checkout. It runs the packaged
`leo-dashboard` entry point from a root-owned virtual environment below
`/opt`, loads only the dashboard login credential, and uses a dynamic service
identity. `ProtectHome=yes` hides the current Gauss CAS and capture state,
`PrivateDevices=yes` hides the radios, and the unit grants no state directory,
supplementary group, bind mount, or writable path. The adapter assumes only
`leo_dashboard` and makes every query transaction read-only.

Build one reviewed wheel and export only the server dependency closure from the
locked dependency graph. Do this from the exact approved worktree before using
administrator privileges:

```console
gauss_dashboard_stage=$(mktemp -d)
uv build --wheel --out-dir "$gauss_dashboard_stage"
uv export --frozen --no-dev --extra server --no-emit-project \
  --format requirements.txt \
  --output-file "$gauss_dashboard_stage/server-requirements.txt"
mapfile -t gauss_dashboard_wheels \
  < <(find "$gauss_dashboard_stage" -maxdepth 1 -type f \
    -name 'leo_tracker_redux-*.whl' -print)
test "${#gauss_dashboard_wheels[@]}" -eq 1
gauss_dashboard_wheel=${gauss_dashboard_wheels[0]}
gauss_dashboard_release_id=$(sha256sum "$gauss_dashboard_wheel" | cut -d' ' -f1)
gauss_dashboard_release_root=/opt/leo-flow-dashboard-$gauss_dashboard_release_id
sha256sum "$gauss_dashboard_wheel" uv.lock \
  "$gauss_dashboard_stage/server-requirements.txt"
```

Review and retain those three digests. The following is the only privileged
installation boundary for the current Gauss host. It deliberately refuses to
replace an existing release directory or `/opt/leo-flow` link; an upgrade must
stage a new digest-qualified release and receive its own review.

```console
sudo test ! -e "$gauss_dashboard_release_root"
sudo test ! -e /opt/leo-flow
sudo install -d -o root -g root -m 0755 /etc/leo-flow /etc/leo-flow/secrets
sudo /home/mouse9911/.local/bin/uv venv \
  --python /usr/bin/python3.14 "$gauss_dashboard_release_root"
sudo /home/mouse9911/.local/bin/uv pip install \
  --python "$gauss_dashboard_release_root/bin/python" \
  --require-hashes -r "$gauss_dashboard_stage/server-requirements.txt"
sudo /home/mouse9911/.local/bin/uv pip install \
  --python "$gauss_dashboard_release_root/bin/python" \
  --no-deps "$gauss_dashboard_wheel"
sudo ln -s "$gauss_dashboard_release_root" /opt/leo-flow
sudo install -o root -g root -m 0644 \
  deploy/dashboard-v1/dashboard.json /etc/leo-flow/dashboard.json
sudo install -o root -g root -m 0600 \
  /home/mouse9911/.local/state/leo-flow/credentials/gauss-dashboard/catalog-dsn \
  /etc/leo-flow/secrets/dashboard-catalog-dsn
sudo install -o root -g root -m 0644 \
  deploy/dashboard-v1/leo-dashboard.service \
  /etc/systemd/system/leo-dashboard.service
sudo systemd-analyze verify /etc/systemd/system/leo-dashboard.service
sudo systemctl daemon-reload
/opt/leo-flow/bin/leo-dashboard --help >/dev/null
sudo systemctl show leo-dashboard.service \
  -p FragmentPath -p LoadState -p ActiveState -p ExecStart
```

At this point the persistent unit is installed and verified but has not taken
the listener. Stop only the known user-manager transient unit, then start the
system unit. Do not kill an arbitrary process holding port 8090.

```console
systemctl --user show leo-dashboard.service \
  -p UnitFileState -p FragmentPath -p MainPID -p ExecStart
systemctl --user stop leo-dashboard.service
sudo systemctl enable --now leo-dashboard.service
sudo systemctl is-active leo-dashboard.service
sudo systemctl show leo-dashboard.service \
  -p UnitFileState -p FragmentPath -p MainPID -p ExecStart
ss -ltnp | rg ':8090'
curl --fail --silent http://127.0.0.1:8090/api/storage-health
curl --fail --silent http://192.168.1.142:8090/api/storage-health
curl --fail --silent http://100.105.69.63:8090/api/storage-health
```

The successful cutover must show a system fragment below
`/etc/systemd/system`, the `/opt/leo-flow/bin/leo-dashboard` entry point, one
listener on `0.0.0.0:8090`, and successful queries through every intended
interface. If the system unit does not become ready, stop only that unit and
restore the reviewed transient command while the retained release and journal
evidence are inspected; do not modify the credential, database roles, CAS, or
capture state during rollback.

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
belong to that reverse proxy. Do not forward credentials to the browser or
cache `/api/` responses. Verify `/`, one `/api/storage-health` request,
authentication rejection, TLS, and the CSP after proxy installation.

## Browser end-to-end proof

The Playwright proof starts the real `StdlibDashboardServer` on an ephemeral
loopback port, serves the packaged operator interface through
`DashboardUiApplication`, and drives Chromium without request interception.
Its repository is an in-memory implementation of the public dashboard query
port populated with deterministic DTO fixtures; the browser and server never
open a CAS object, recording file, radio, or database connection.

Install the locked development dependencies and matching browser once per
machine. The dependency command may require administrator access because it
installs Chromium's Ubuntu shared libraries:

```console
uv sync --frozen --extra dev
uv run playwright install --with-deps chromium
```

Run the browser proof with:

```console
uv run pytest -q tests/e2e/test_dashboard_browser.py
```

The proof covers initial activity, recording, track, and storage refreshes;
recording detail and independent feature loading; exact model and detector
evaluation lookups; and browser-visible ready, partial, missing-object, and
not-found error states. It uses Chromium's real `fetch` implementation against
the same-origin JSON routes and does not fulfill or rewrite browser requests.

For a local one-request smoke test under a systemd credential environment:

```console
leo-dashboard --config /etc/leo-flow/dashboard.json --once
```

One-shot mode intentionally exits after one request or one finite idle wait.
Importing the plugin reads no credential, imports no optional PostgreSQL driver,
opens no database connection, and binds no socket. Those actions occur only
after complete bootstrap validation, during credential resolution, adapter
selection, listener preflight, and request handling respectively.
