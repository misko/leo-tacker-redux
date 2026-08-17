# Gauss `.20`/`.21` user-systemd launch runbook

Status: offline deployment template only. Nothing in this document authorizes
installing, enabling, or starting a service before the selected sealed release and the main
definition are sealed and independently verified.

## Why user services

The capture and analysis processes must run as `mouse9911`, which owns the
credential, campaign-state, and CAS roots. The host does not currently provide
non-interactive sudo, so installing system services cannot be made atomic by
the unattended release process. A user unit also avoids creating root-owned
files beneath the user-owned `0700` state root.

The templates intentionally omit `User=` and `Group=` because a user manager
already runs the service as its owner. The host's user manager cannot create
the namespace/capability sandbox used by system services (`218/CAPABILITIES`),
so these user units deliberately rely on the unprivileged account, an exact
closed-tree verifier before every invocation, absolute immutable input paths,
`UMask=0077`, and bounded resources instead of unsupported namespace directives.

## Unrendered inputs

The two `.service.in` files are deliberately not installable. The integration
steward must replace every token below with an absolute, already existing
value and then retain the rendered unit bytes in release evidence:

| Token | Required value |
|---|---|
| `@RELEASE_ROOT@` | selected sealed release root containing its manifest, validation receipt, venv, configs, and vendored native runtime |
| `@RELEASE_MANIFEST_SHA256@` | lowercase 64-hex SHA-256 of the selected release manifest bytes |
| `@RELEASE_RECEIPT_SHA256@` | lowercase 64-hex SHA-256 of the selected release validation receipt bytes |
| `@MAIN_CAMPAIGN_ROOT@` | fresh state root for the one reviewed 936-slot campaign |
| `@MAIN_DEFINITION_PATH@` | absolute path of the campaign's immutable definition; it need not be inside the mutable state root |
| `@MAIN_DEFINITION_DIGEST@` | exact `sha256:...` digest emitted for that root's immutable definition |
| `@QUALIFICATION_RECEIPT_PATH@` | absolute path of the exact successful qualification receipt named by the main definition |

The selected release must vendor libiio, its Python binding, and the reviewed SPF modules
inside the release at the paths used by the templates. A writable `.cache`
path is not a release input and must not appear in either rendered unit.
Every service start first runs the selected release's offline verifier against the exact
manifest and validation-receipt SHAs. The verifier must reject a receipt
mismatch, failed or live-
contact validation, any missing/extra closed-tree entry, any byte/size change,
and any symlink escape before the capture or analysis entrypoint can execute.

## Mandatory ordered gates

| Order | Gate | Required evidence |
|---:|---|---|
| 1 | Release sealed | manifest and validation receipt re-hash exactly; entire release tree is non-writable; import inventory resolves only inside the selected release |
| 2 | Qualification accepted | selected receipt decodes canonically, names the exact station/runtime identities and nine successful cells, and its digest equals the main definition's `qualification_receipt_digest` |
| 3 | Database promoted | migration receipts exactly match approved files through `0040_dashboard_doppler_aggregate.sql`; initial drain and registered-terminal concurrent-analysis gates are true |
| 4 | Exclusive host | no campaign, capture, analysis, or process-mode lock owner; no process has an established session to `.20` or `.21` |
| 5 | Capacity | available bytes at both state and CAS roots are at least `75,966,218,240` immediately before arming |
| 6 | Credentials | capture, analysis, and dashboard `catalog-dsn` files exist, are owned by `mouse9911`, and are mode `0600` below the existing `0700` credential root |
| 7 | Main definition | fresh 936-slot deferred-analysis definition; exact 36-slot balance repeated 26 times; raw-byte and transition limits match the reviewed policy; no placeholder remains |
| 8 | Rendered units | `systemd-analyze --user verify` passes; unit evidence contains no checkout, `.cache`, v5 receipt, `.17`, `.18`, or replacement token |
| 9 | User-manager durability | `loginctl show-user mouse9911 -p Linger` reports `Linger=yes`; user manager is healthy enough to start both units |
| 10 | Radio preflight | exact `.20`/`.21` serial and V5 runtime; both TX gains at most `-80 dB`; all DDS scales zero; constant-IQ and continuity gates pass |

Do not enable lingering before the final rendered units pass their offline
verification. On this host enabling linger requires administrator mediation;
`sudo -n` is unavailable. Do not compensate by running capture as root.

## Installation and launch

After all gates pass, copy the two rendered (not `.in`) files to
`/home/mouse9911/.config/systemd/user/`, reload the user manager, and enable
only the capture unit. The capture unit's `OnSuccess=` edge starts staged
analysis only after capture exits successfully. Do not independently enable
the analysis unit.

The capture command returns retryable status `75` for a bounded pending slice
or temporary capacity block. `RestartSec=1s` is below the reviewed
`6.043615s` margin between the slowest qualified publication and the next
`T-15s` preflight boundary, leaving more than five seconds before that
boundary. Exit statuses `2`, `3`, and `4` remain fail-closed and never restart;
a terminal capture failure therefore cannot activate analysis through
`OnSuccess=`.

The analysis command also uses retryable status `75`; it has no RF deadline and
restarts from its fenced SQLite/PostgreSQL state after one second. It processes
26 exact 36-batch windows with at most eight compute and four projection
workers, then performs the final phase-close transition.

## Stop and recovery semantics

Stopping capture is an operator intervention, not a pause contract. Inspect
the sanitized `status` command and journal before any restart. Never move a
missed slot, change the definition, manufacture a qualification receipt, or
reuse a halted campaign identity. A failed/halted campaign preserves all
published recordings and requires a separately reviewed future identity.
