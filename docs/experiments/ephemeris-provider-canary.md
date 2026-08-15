# Ephemeris provider canary evidence

Evidence date: 2026-08-14. This record deliberately separates executable
fixture evidence from live-provider evidence.

| Claim | Evidence in this wave | Result |
|---|---|---|
| Immutable provider input path | Deterministic Hugging Face-format fixture archived as raw, normalized, and provenance CAS objects | Pass |
| Repeated periodic behavior | Same-root and independent-root repetitions produce the same four content identities and receipt digest | Pass |
| Cataloging | The newly published exact snapshot reference is resolved from the catalog before propagation | Pass |
| SGP4 | Archived normalized bytes are reopened by exact catalog identity and propagated with `sgp4==2.25`, Vallado improved mode, WGS72, and the pinned time/Earth-orientation/error profiles | Pass |
| Supervision | Checked-in hardened oneshot and persistent six-hour systemd timer; installed service denies Internet address families | Pass (configuration evidence) |
| Failure/retry | Focused scheduler/worker tests cover stable slot IDs, replay idempotence, provider `Retry-After`, capped transient backoff, authentication parking, and secret-free reason codes | Pass (fixture/component evidence) |
| Hugging Face live retrieval | No reviewed config with `network_approved: true` was present | **Not run; missing** |
| Space-Track live retrieval | No approved config and no existing dedicated named credential capability were present | **Not run; missing** |

The deterministic receipt object is
`sha256:6c8d3025250b0be8e52bf4b9a02ae08353e36db8d29325d06c0fa169dfdbe3e4`
with format `ephemeris-provider-canary-receipt-v1`. It records
`mode=fixture` and `live_retrieval_performed=false`; it is not evidence that an
external provider was reachable or current.

The implementation/network boundary is tested with injected HTTP transports.
Those tests exercise the dual approval gate, one-request ceiling, persistent
minimum interval, exact named Space-Track credential resolution, and absence of
credential values from receipts. Because the HTTP transport is injected, those
receipts also record `live_retrieval_performed=false`.

To produce legitimate live Hugging Face evidence, an operator must review the
endpoint and provider terms, copy the example config outside the repository,
set `network_approved` to `true`, install the explicit network override, and
retain the resulting receipt. Space-Track additionally requires pre-provisioned
dedicated systemd credentials whose exact capability names match the reviewed
config. No credential directory discovery or fallback environment lookup is
part of this workflow.
