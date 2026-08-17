# Radio lifecycle detection v0.1

Status: host integration implemented and verified. Runtime enablement is gated
on a firmware release that implements the authenticated diagnostic v0.1 wire
contract and per-radio key enrollment. The current radios and capture runtime
are not contacted or mutated by this change.

## Outcome and boundary

The capture pipeline needs to distinguish a full radio reboot from an iiOD
restart, an AD9361 reinitialization, and an unexplained transport timeout. A
broken socket is not reboot evidence. The v0.1 classifier makes a reboot claim
only when two authenticated observations for the same radio contain different
Linux `boot_id` values.

The capture component consumes the narrow `RadioLifecycleObserverV0_1` port. It
does not invoke SSH, read `/proc`, parse logs, construct firmware paths, or know
how an adapter obtains evidence. The observer returns one bounded contract or a
sanitized unavailable reason. Persistence consumes immutable attempt, batch,
and between-slot facts through `RadioLifecycleFactRecorderV0_1`.

## Radio-side source of truth

The preferred source is a read-only authenticated diagnostic endpoint owned by
the radio firmware. A request contains a protocol version, radio identity,
nonce, and host deadline. The response is bounded to 2 KiB and contains:

| Field | Source | Identity use |
|---|---|---|
| Linux `boot_id` | `/proc/sys/kernel/random/boot_id` | Full reboot authority |
| monotonic boot uptime, ns | `CLOCK_BOOTTIME` | Operator evidence only |
| estimated boot UTC + uncertainty | radio observation UTC minus uptime | Display only; never identity |
| iiOD PID + `/proc/<pid>/stat` start ticks + `CLK_TCK` | firmware supervisor | iiOD process identity |
| AD9361 initialization epoch | monotonic firmware counter | AD9361 reset authority when present |
| bounded reset reason | firmware enum | Explanation only |

The response must authenticate the full canonical payload, nonce, radio serial,
and protocol version with a per-device credential or enrolled device key. It
must not execute input, accept paths, return logs, or return environment data.
The host verifies identity, nonce, signature/MAC, response size, schema, and
deadline before constructing a `radio_authenticated` observation.

`spf-radio-metadata-v4` may carry the same signed lifecycle block in each refill
header. That is the best mid-capture evidence because it shares the data path.
Until firmware provides either source, a separately packaged authenticated host
fallback may read a fixed allowlist of fields. Its adapter is the only place SSH
may exist: fixed command, no user fragments, host-key pinning, dedicated
read-only account, bounded output/time, no shell interpolation, no raw-output
persistence. The capture component sees the same port in all cases.

## Classification and failure semantics

Classification precedence is deterministic:

| Evidence | Stable reason | Confidence |
|---|---|---|
| changed authenticated `boot_id` | `radio_rebooted` | high |
| same boot, changed iiOD `(pid,start_ticks,CLK_TCK)` | `iiod_restarted` | high |
| same boot/iiOD, changed AD9361 epoch | `ad9361_reinitialized` | high |
| timeout/disconnect without a provable identity change | `transport_timeout_unknown` | low |
| no change or observer unavailable without transport failure | no diagnosis | none |

PID alone is never compared. Estimated boot UTC, uptime regression/wrap, wall
clock changes, firmware version, TX gain, and network reachability are never
boot identity. If multiple generations change, the broadest event wins:
`radio_rebooted`, then `iiod_restarted`, then `ad9361_reinitialized`.

At attempt preflight, capture obtains an observation before declaring ready. On
normal completion it obtains another before releasing the radio owner. On
disconnect or timeout it immediately makes one bounded observation attempt and
then seals the terminal lifecycle fact; diagnosis failure must never delay
capture cleanup. A changed boot makes the attempt terminally failed even if
some samples were received. A missing observer retains the transport failure
and low-confidence unknown reason; it never manufactures a reboot.

The previous terminal observation and next preflight observation form a
`RadioLifecycleIntervalFactV0_1`, detecting changes between immutable slots. A
between-slot reboot blocks that slot until passive TX/DDS and exact radio
attestation are re-established. It does not rewrite the previous attempt.

## Public contracts and API versioning

All types are additive schema v0.1 contracts in
`leo_flow.contracts.radio_lifecycle`; published capture-batch v0.1 remains
unchanged. Exact fact replay is idempotent; a different value for the same
attempt or interval key is a conflict.

The proposed additive capture-detail endpoint is:

`GET /api/v5/capture-attempts/{attempt_id}/radio-lifecycle`

It returns `org.leo-flow.dashboard.capture-attempt-radio-lifecycle` v0.1:
reason, confidence, fixed evidence codes, preflight/terminal boot IDs and
uptimes, and terminal observer availability. It contains no raw logs, commands,
addresses, credentials, exception strings, or reset detail outside fixed codes.
Consumers must reject unknown major versions. A field change requires a new
schema version; existing published versions are immutable.

## Threat model

- A network attacker must not forge a reboot: authenticate the radio response,
  bind it to radio ID and nonce, and reject replayed/noncanonical responses.
- A compromised radio can lie about itself; dashboard trust identifies the
  evidence source. Cross-checking power telemetry is a separate signal, not a
  substitute for boot identity.
- A caller cannot turn the endpoint into remote command execution or file read:
  there are no command/path parameters and outputs are allowlisted and bounded.
- Time correction cannot fake a reboot because time is never an identity key.
- PID reuse cannot fake continuity because process start ticks are part of the
  iiOD identity.
- Diagnostic failure cannot erase capture evidence. It yields an unavailable
  observation and retains the original transport outcome.
- Public facts and API responses contain no secrets, raw logs, environment,
  stack traces, or unbounded firmware strings.

## Exact component test matrix

| Case | Required result |
|---|---|
| same boot/process/AD epoch | no lifecycle diagnosis |
| changed boot ID during failure | `radio_rebooted`, high |
| changed boot ID between slots | immutable interval reboot fact |
| uptime regression/wrap or wall-clock skew, same IDs | no reboot |
| observer unavailable + timeout/disconnect | `transport_timeout_unknown`, low |
| observer unavailable, no transport failure | no claim |
| same boot, iiOD PID/start identity changes | `iiod_restarted`, high |
| PID reused with new start ticks | `iiod_restarted`, high |
| same boot/iiOD, AD9361 epoch changes | `ad9361_reinitialized`, high |
| transport timeout with unchanged identities | unknown, never reboot |
| exact fact replay | idempotent |
| conflicting fact replay | rejected |
| malformed UUID/incomplete identity | rejected |
| raw or unbounded unavailable reason | rejected |
| dashboard projection | bounded lifecycle fields only |

## Integrated inventory and rollout gate

Implemented host-side:

1. `AuthenticatedRadioLifecycleObserverV0_1` creates a unique nonce, enforces a
   two-KiB response bound and deadline, verifies HMAC-SHA256 over canonical JSON,
   binds protocol/radio/nonce, and returns sanitized unavailable observations.
2. `LifecycleObservedAttemptWorkV0_1` observes before radio attestation and
   readiness, reuses the normal exact V5 attestation afterward, observes after
   normal or failed terminal cleanup, and records between-slot and attempt facts.
3. Migration 0031 owns immutable fact tables, exact-replay/conflict routines,
   narrow capture/dashboard grants, history lookup, and bounded fact lookup.
4. The additive V5 dashboard route and capture-row disclosure show only typed
   lifecycle evidence. Missing facts are explicit and do not imply continuity.
5. Component, adapter, cross-component, fresh-PostgreSQL, API, and Playwright
   browser tests cover the host path. A real-radio canary remains a rollout gate,
   not a unit-test substitute.

Firmware rollout remains intentionally separate. The next firmware must provide
the fixed diagnostic responder, per-device key enrollment, nonce replay cache,
Linux boot ID and iiOD process identity, plus the monotonic AD9361 initialization
epoch. After firmware installation, configure the existing credential mechanism,
enable the lifecycle work decorator in the capture release, apply migration 0031,
restart the dashboard on the V5 composition, and run a two-slot canary containing
one controlled radio reboot. Roll back by disabling the decorator; immutable
facts and the additive read endpoint may remain safely deployed.
