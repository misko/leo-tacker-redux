# leo-tracker-redux

A deliberately small rebuild of the LEO radio experiment pipeline around three
applications: capture, analysis, and dashboard.

The architecture is contract-first. Capture publishes immutable recordings,
independent analysis maps one recording to a feature bundle, model analysis fits
cross-recording state from frozen feature sets, and the dashboard is read-only.

The checked operator surface provides separate commands for each process:

- `leo-v5-capture` for restart-safe single-radio V5 capture;
- `leo-v5-dual-capture` for independent or software-coordinated dual capture;
- `leo-v5-campaign` for the finite, checkpointed Gauss `.20`/`.21` campaign;
- `leo-gauss-analysis` for deferred local submission, analysis, and projection;
- `leo-dashboard` for the read-only loopback web interface.

The current Gauss development profiles bind exact identities at
`192.168.1.15`, `192.168.1.20`, and `192.168.1.21`. IP addresses are routing
inputs, never radio identity; live commands also require the independently
observed serial and immutable plan/batch digests. Follow the checkpointed
[Gauss local pipeline runbook](docs/operations/gauss-local-pipeline.md).

The legacy `leo-tracker` repository remains a reference and numerical oracle,
not a runtime dependency.

V5 Pluto capture uses the isolated, fail-closed host environment documented in
[Pluto V5 host runtime](docs/v5-host-runtime.md).
