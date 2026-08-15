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
| Hugging Face live retrieval | One explicitly approved, bounded request archived 10,753 TLEs and produced an exact catalog-to-SGP4 receipt | Pass |
| Space-Track live retrieval | No approved config and no existing dedicated named credential capability were present | **Not run; missing** |

The deterministic receipt object is
`sha256:779f7255d2f5cfe383c7865ec734b86b1cb24d157e94d2abc9e1ad9a5f568917`
with format `ephemeris-provider-canary-receipt-v1`. It records
`mode=fixture` and `live_retrieval_performed=false`; it is not evidence that an
external provider was reachable or current.

The live Hugging Face run on 2026-08-14 local time retrieved 1,806,504 bytes
containing 10,753 checksum-valid Starlink TLEs. It published snapshot
`eph_1d509fee9a7e0d7e6361af4ae1d6c20aaf3a7307d6e29011b78ca485999ada2`
and receipt
`sha256:1f2b61bff2bfe5155751ba707ce20592251f84a40afb824a68b7243414b3f865`,
whose `mode=network` and `live_retrieval_performed=true`. The raw, normalized,
provenance, and receipt objects are preserved under
`/var/tmp/leo-ephemeris-hf-live-20260814`. This proves the public Hugging Face
path, not authenticated Space-Track access.

On the analysis host, the user-scoped `leo-ephemeris-live.timer` was enabled
and active after this proof. It uses a persistent six-hour calendar, a five-
minute randomized delay, the same CAS/rate root, and the same dual-gated live
canary command. At verification time its next trigger was 2026-08-15 03:02 PDT.
The host-local unit and reviewed configuration live under
`~/.config/systemd/user` and `~/.config/leo-flow`; the checked-in system units
remain the portable deployment source.

The live check also found two provider-compatibility facts now covered by
tests: Hugging Face's `resolve/main` URL redirects while `raw/main` returns the
same file directly, and the dataset uses conventional unprefixed three-line
TLE title records. Redirect rejection remains intact; the fixed endpoint and
strict parser now accept that standard representation.

The implementation/network boundary is tested with injected HTTP transports.
Those tests exercise the dual approval gate, one-request ceiling, persistent
minimum interval, exact named Space-Track credential resolution, and absence of
credential values from receipts. Because the HTTP transport is injected, those
receipts also record `live_retrieval_performed=false`.

For scheduled live operation, an operator must still install a reviewed config
outside the repository with `network_approved=true` and the explicit network
override. Space-Track additionally requires pre-provisioned dedicated systemd
credentials whose exact capability names match the reviewed config. No
credential directory discovery or fallback environment lookup is part of this
workflow.
