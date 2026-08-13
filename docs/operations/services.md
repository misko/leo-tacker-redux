# Service process composition

Redux has three independently supervised process boundaries. This layer owns
process lifecycle only; it does not own a workflow engine or construct external
systems.

| Process | Injected unit | Allowed knowledge | Explicitly excluded |
|---|---|---|---|
| capture | `CaptureCycle` | plans, preflight, radio, recording writer, local spool, recording publication | analysis, models, dashboard |
| analysis | `AnalysisCycle` | fenced jobs, published recording readers, feature/model publication | radio and capture implementations |
| dashboard | `ReadOnlyDashboardServer` and `JsonDashboardHandler` | read/query projection | writers, jobs, capture and analysis implementations |

The process host resolves the adapter references in configuration and injects
the resulting objects. Adapter construction stays outside `leo_flow.services` so
tests can use safe fakes and capture cannot acquire a database or network
capability that was not deliberately granted.

## Configuration v1

Configuration is JSON, uses `schema_version: 1`, and rejects every missing or
unknown key. Secrets are never values in this document. A `secret_refs` entry
identifies a value that the deployment host may resolve from an explicitly
configured provider.

```json
{
  "schema_version": 1,
  "process": "capture",
  "runtime": {
    "instance_id": "station-a-capture-1",
    "poll_interval_s": 0.25,
    "shutdown_timeout_s": 10.0,
    "secret_refs": [
      {"provider": "systemd-credential", "name": "catalog-dsn"}
    ]
  },
  "adapters": {
    "plan_source_ref": "plans.postgres-v1",
    "radio_ref": "pluto-v5.radio-a",
    "preflight_ref": "pluto-v5.attestation-v1",
    "recording_writer_ref": "sigmf.local-v1",
    "spool_ref": "sqlite.local-v1",
    "recording_publisher_ref": "cas-catalog.postgres-v1"
  }
}
```

Analysis adapters are `job_repository_ref`, `recording_reader_ref`,
`feature_publisher_ref`, and `model_publisher_ref`. Dashboard adapters are
`query_projection_ref`, `server_ref`, `bind_host`, and `bind_port`. Adapter
references are exact deployment selections; there is no `latest` resolution.

## Lifecycle and supervision

`ServiceLoop.run_once()` performs one capture/publication unit, one fenced
analysis job, or one server event. It is the deterministic one-shot mode used by
tests and maintenance commands. `run_forever()` repeats those units and waits
without busy looping when idle.

Startup invokes the injected preflight once even if several one-shot calls are
made. Readiness is true only in `ready`. SIGTERM and SIGINT stop admission of new
units and immediately transition health to `draining`. After the active bounded
unit returns, the loop calls the adapter close hook. Capture and analysis
adapters must therefore give each radio/read/publish operation its own finite
I/O deadline; process shutdown is not a mechanism for safely killing Python
inside an arbitrary blocking driver call. The close hook
receives the configured timeout and is guarded by the same hard deadline; an
overrun leaves health in `failed` and raises `ServiceLifecycleError`. Repeated
clean shutdown is idempotent.

Every lifecycle transition and completed or failed unit can be emitted as one
compact JSON object per line. Diagnostics contain service identity, state,
counts, and a sanitized exception description. Configuration and secret
references are not included.

The concrete capture operation must finish or durably abort its current
recording before returning. The fenced analysis helper completes a lease only
after its processor returns the immutable result reference; failures are first
recorded through the active token and generation and then propagated for the
supervisor to restart. The dashboard receives only the existing read-only JSON
handler.

## Deployment wiring still required

The integration/deployment layer must provide executable bootstraps that:

1. load and validate one configuration document;
2. resolve its named adapters from an immutable deployment manifest;
3. resolve each secret reference through the selected secret provider;
4. construct only the capabilities allowed to that process;
5. attach `JsonLineDiagnosticSink` to standard output;
6. run preflight and then `run_forever()` under systemd or a container runtime.

Console entry points are intentionally not registered yet. Registering a CLI
before those production adapter assemblies exist would create a command that
can validate configuration but cannot safely run a service. No NFS files,
shell orchestration, legacy repository imports, or ambient model aliases are
part of this design.
